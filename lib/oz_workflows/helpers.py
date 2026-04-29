from __future__ import annotations

import json
import logging
import re
import urllib.parse
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from github import Github
from github.GithubException import UnknownObjectException
from github.IssueComment import IssueComment
from github.PullRequest import PullRequest
from github.PullRequestComment import PullRequestComment
from github.Repository import Repository
from oz_agent_sdk.types.agent import RunItem

from . import actions
from .artifacts import ResolvedReviewComment
from .env import optional_env, workspace
from .workflow_config import load_triage_workflow_config

logger = logging.getLogger(__name__)


# Author associations that indicate organization membership.
ORG_MEMBER_ASSOCIATIONS: set[str] = {"COLLABORATOR", "MEMBER", "OWNER"}

_CLOSING_ISSUES_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $after: String) {"
    " repository(owner: $owner, name: $name) {"
    " pullRequest(number: $number) {"
    " closingIssuesReferences(first: 100, after: $after) {"
    " pageInfo { hasNextPage endCursor }"
    " nodes {"
    " number"
    " repository { owner { login } name }"
    " }"
    " }"
    " }"
    " }"
    " }"
)

_MANUAL_LINKED_ISSUES_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $after: String) {"
    " repository(owner: $owner, name: $name) {"
    " pullRequest(number: $number) {"
    " timelineItems(first: 100, after: $after, itemTypes: [CONNECTED_EVENT, DISCONNECTED_EVENT]) {"
    " pageInfo { hasNextPage endCursor }"
    " nodes {"
    " __typename"
    " ... on ConnectedEvent {"
    " subject {"
    " __typename"
    " ... on Issue {"
    " number"
    " repository { owner { login } name }"
    " }"
    " }"
    " }"
    " ... on DisconnectedEvent {"
    " subject {"
    " __typename"
    " ... on Issue {"
    " number"
    " repository { owner { login } name }"
    " }"
    " }"
    " }"
    " }"
    " }"
    " }"
    " }"
    " }"
)


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def get_field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def get_login(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("login") or "")
    return str(getattr(item, "login", "") or "")


def is_automation_user(user: Any) -> bool:
    """Return whether *user* is an automation account that should not trigger workflows."""
    login = get_login(user).strip().lower()
    user_type = str(get_field(user, "type", "") or "").strip().lower()
    return (
        user_type == "bot"
        or (bool(login) and login.endswith("[bot]"))
    )


def is_trusted_commenter(
    github_client: Github,
    event: dict[str, Any],
    *,
    org: str,
) -> bool:
    """Decide whether the commenter that triggered *event* is trusted.

    Trust is evaluated deterministically in Python before the agent runs so
    we can short-circuit untrusted mentions without relying on the agent to
    infer trust from the presence or absence of the triggering comment in
    ``fetch_github_context.py`` output (the fetch output can legitimately
    miss the comment for reasons unrelated to trust: script path issues,
    transient API errors, pagination edge cases, output truncation, etc.).

    Trust rules mirror the ``check_trust`` job in
    ``respond-to-pr-comment-local.yml`` and the org-membership fallback in
    ``fetch_github_context.py``:

    - If the comment's ``author_association`` is ``OWNER``, ``MEMBER``, or
      ``COLLABORATOR`` the author is trusted immediately.
    - Otherwise probe ``GET /orgs/{org}/members/{login}``. A 204 response
      promotes the author to trusted so legitimate org members whose
      membership is private (or whose association the event payload
      reports as ``CONTRIBUTOR`` for any other reason) are not dropped.
    - Any other status (404 "not a member", 302 redirect to the public
      endpoint, request error, ...) leaves the author untrusted. We fail
      closed on errors to avoid accidentally granting trust.
    """
    # Support both comment events (event["comment"]) and review events (event["review"]).
    actor = event.get("comment") if isinstance(event, dict) else None
    if not isinstance(actor, dict):
        actor = event.get("review") if isinstance(event, dict) else None
    if not isinstance(actor, dict):
        return False
    association = str(actor.get("author_association") or "").upper()
    if association in ORG_MEMBER_ASSOCIATIONS:
        return True
    login = (actor.get("user") or {}).get("login") or ""
    if not login or not org:
        return False
    path = (
        f"/orgs/{urllib.parse.quote(org, safe='')}"
        f"/members/{urllib.parse.quote(login, safe='')}"
    )
    try:
        status, _headers, _body = github_client.requester.requestJson(
            "GET", path
        )
    except Exception:
        logger.exception(
            "Org membership probe for @%s in %s failed; treating author as untrusted.",
            login,
            org,
        )
        return False
    return status == 204


def get_timestamp_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value or "")


def get_label_name(label: Any) -> str:
    if isinstance(label, str):
        return label
    return str(get_field(label, "name", "") or "")


def format_issue_comments_for_prompt(
    comments: list[Any],
    *,
    metadata_prefix: str,
    exclude_comment_id: int | None = None,
) -> str:
    """Format human-authored issue comments for prompt context.

    ``metadata_prefix`` is kept in the signature for backwards compatibility
    with older callers, but the filtering decision no longer depends on
    scanning comment bodies for Oz metadata markers. Instead, we drop all
    automation-authored comments so bot messages are excluded even when they
    do not carry a metadata footer, and human-authored comments remain visible
    even if they happen to contain the metadata prefix text.
    """
    selected = [
        comment
        for comment in comments
        if int(get_field(comment, "id") or 0) != exclude_comment_id
        and not is_automation_user(get_field(comment, "user"))
    ]
    if not selected:
        return "- None"
    formatted = []
    for comment in selected:
        user = get_login(get_field(comment, "user")) or "unknown"
        association = get_field(comment, "author_association") or "NONE"
        body = str(get_field(comment, "body") or "").strip() or "(no body)"
        formatted.append(
            f"- @{user} [{association}] ({get_timestamp_text(get_field(comment, 'created_at'))}): {body}"
        )
    return "\n".join(formatted)


def _filter_review_comments_in_thread(
    all_review_comments: list[Any],
    trigger_comment_id: int,
) -> list[Any]:
    """Return review comments that belong to the thread containing *trigger_comment_id*.

    GitHub's REST API (and therefore PyGitHub) does not expose an endpoint for
    fetching a single review thread by comment id; ``pullRequestReviewThread``
    exists only in the GraphQL API. ``PullRequest.get_review_comment(id)``
    returns just the one comment, and ``get_single_review_comments(review_id)``
    scopes to a ``PullRequestReview`` batch rather than a reply thread, so we
    have to filter client-side.

    GitHub flat-threads review replies: every reply's ``in_reply_to_id`` points
    directly at the thread root regardless of which comment was quoted, so the
    root is either the triggering comment itself or the comment its
    ``in_reply_to_id`` refers to.
    """
    by_id: dict[int, Any] = {int(get_field(c, "id")): c for c in all_review_comments}
    trigger = by_id.get(trigger_comment_id)
    parent = get_field(trigger, "in_reply_to_id") if trigger is not None else None
    root_id = int(parent) if parent is not None else trigger_comment_id
    return [
        c
        for c in all_review_comments
        if int(get_field(c, "id")) == root_id or get_field(c, "in_reply_to_id") == root_id
    ]


def org_member_comments_text(
    comments: list[Any],
    *,
    exclude_comment_id: int | None = None,
) -> str:
    selected = [
        comment
        for comment in comments
        if get_field(comment, "author_association") in ORG_MEMBER_ASSOCIATIONS
        and int(get_field(comment, "id") or 0) != exclude_comment_id
    ]
    if not selected:
        return ""
    return "\n".join(
        f"- {get_login(get_field(comment, 'user')) or 'unknown'} ({get_timestamp_text(get_field(comment, 'created_at'))}): {get_field(comment, 'body') or ''}"
        for comment in selected
    )


def triggering_comment_prompt_text(event_payload: dict[str, Any]) -> str:
    comment = event_payload.get("comment")
    if not isinstance(comment, dict):
        return ""
    body = str(comment.get("body") or "").strip()
    if not body:
        return ""
    author_login = (comment.get("user") or {}).get("login") or (event_payload.get("sender") or {}).get("login") or "unknown"
    return f"@{author_login} commented:\n{body}"


def comment_metadata(
    workflow: str,
    issue_number: int,
    *,
    run_id: str = "",
    oz_run_id: str = "",
    github_run_id: str = "",
) -> str:
    payload: dict[str, Any] = {
        "type": "issue-status",
        "workflow": workflow,
        "issue": issue_number,
    }
    if run_id:
        payload["run_id"] = run_id
    if github_run_id:
        payload["github_run_id"] = github_run_id
    if oz_run_id:
        payload["oz_run_id"] = oz_run_id
    return f"<!-- oz-agent-metadata: {json.dumps(payload, separators=(',', ':'))} -->"


def _workflow_metadata_prefix(workflow: str, issue_number: int) -> str:
    """Return the stable metadata prefix shared by all runs of the same workflow on an issue."""
    return f'<!-- oz-agent-metadata: {{"type":"issue-status","workflow":"{workflow}","issue":{issue_number}'


def _strip_workflow_metadata(body: str, workflow_prefix: str) -> str:
    """Remove any metadata marker in *body* whose prefix matches *workflow_prefix*.

    The progress comment metadata marker is rebuilt mid-run when additional
    identifiers (e.g. the Oz run id) become available. This helper strips any
    existing marker for the same workflow+issue so callers can rebuild the
    body with the current metadata.
    """
    if not body or not workflow_prefix:
        return body
    start = body.find(workflow_prefix)
    if start == -1:
        return body
    end = body.find("-->", start)
    if end == -1:
        return body
    end += len("-->")
    return (body[:start] + body[end:]).strip()


def _parse_workflow_metadata(body: str, workflow_prefix: str) -> dict[str, Any] | None:
    """Parse the workflow metadata marker from *body* when it matches *workflow_prefix*."""
    if not body or not workflow_prefix:
        return None
    start = body.find(workflow_prefix)
    if start == -1:
        return None
    end = body.find("-->", start)
    if end == -1:
        return None
    marker = body[start:end].strip()
    prefix = "<!-- oz-agent-metadata: "
    if not marker.startswith(prefix):
        return None
    try:
        parsed = json.loads(marker[len(prefix):])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def split_comment_body(body: str, metadata: str) -> tuple[str, str]:
    if metadata and metadata in body:
        content, _, _ = body.partition(metadata)
        return content.strip(), metadata
    return body.strip(), metadata


# Italicized suffix appended to every Oz-authored progress comment and
# auto-generated PR body so readers can tell the message came from the Oz
# agent and click through to the Oz landing page for more context.
POWERED_BY_SUFFIX = "_Powered by [Oz](https://oz.warp.dev)_"


def build_comment_body(content: str, metadata: str) -> str:
    content = content.strip()
    # Strip any previously-appended suffix so the one added below stays a
    # single, trailing section regardless of how many times the body is
    # rebuilt (e.g. across append/edit cycles).
    if content.endswith(POWERED_BY_SUFFIX):
        content = content[: -len(POWERED_BY_SUFFIX)].rstrip()
    if content:
        content = f"{content}\n\n{POWERED_BY_SUFFIX}"
    else:
        content = POWERED_BY_SUFFIX
    if metadata:
        return f"{content}\n\n{metadata}"
    return content

_PROGRESS_LINK_PREFIXES = (
    "You can follow along in [the session on Warp]",
    "You can view [the conversation on Warp]",
)


@lru_cache(maxsize=None)
def _configured_prior_triage_labels(workspace_root: str) -> frozenset[str]:
    return load_triage_workflow_config(
        Path(workspace_root)
    ).prior_triage_labels


def issue_has_prior_triage(labels: list[Any]) -> bool:
    """Return True when *labels* indicate a prior triage pass already ran.

    Callers pass the issue's current label objects (or dicts, or plain
    strings) as returned by the GitHub API so we can reuse the same
    ``get_label_name`` helper used elsewhere. Only the ``triaged`` label
    counts, because the triage flow attaches it at the end of every
    completed pass; labels like ``bug``/``enhancement``/``documentation``
    are commonly applied before any triage run and would otherwise cause
    the first triage pass to be misreported as a re-triage.
    """
    configured_labels = _configured_prior_triage_labels(str(workspace().resolve()))
    for label in labels or []:
        if get_label_name(label).lower() in configured_labels:
            return True
    return False


def format_triage_start_line(*, is_retriage: bool) -> str:
    """State-aware opening line for the triage workflow."""
    if is_retriage:
        return (
            "I'm re-triaging this issue based on new information."
        )
    return "I'm starting to work on triaging this issue."


def format_triage_session_line(
    *, is_retriage: bool, session_link_markdown: str
) -> str:
    """Mid-run status line used once the Oz session link is known."""
    verb = "re-triaging" if is_retriage else "triaging"
    return f"I'm {verb} this issue. You can follow {session_link_markdown}."


def format_respond_to_triaged_start_line() -> str:
    """State-aware opening line for the inline triaged-issue response workflow."""
    return (
        "I'm drafting an inline response to this comment. "
        "This issue is already triaged, so I'll reply without changing labels, "
        "the issue body, or assignees."
    )


def format_spec_start_line(*, is_update: bool) -> str:
    """State-aware opening line for the create-spec-from-issue workflow."""
    if is_update:
        return "I'm updating the existing spec PR for this issue."
    return "I'm starting work on product and tech specs for this issue."


def format_spec_complete_line(*, is_update: bool, pr_url: str) -> str:
    """State-aware completion line for the create-spec-from-issue workflow."""
    if is_update:
        return f"I updated the existing [spec PR]({pr_url}) for this issue."
    return f"I created a new [spec PR]({pr_url}) for this issue."


def format_implementation_start_line(
    *,
    spec_context_source: str,
    should_noop: bool,
    existing_implementation_pr: bool,
    unapproved_spec_pr_numbers: list[int] | None = None,
) -> str:
    """State-aware opening line for the implementation workflow.

    *spec_context_source* is the same string produced by
    ``resolve_spec_context_for_issue``: ``approved-pr``, ``directory``,
    or empty for no spec context. *should_noop* indicates that
    unapproved spec PR(s) exist and Oz is refusing to implement them.
    *existing_implementation_pr* signals an already-open draft PR on the
    implementation branch so the comment can say "updating" instead of
    "creating".
    """
    if should_noop:
        numbers = ", ".join(f"#{n}" for n in (unapproved_spec_pr_numbers or []))
        suffix = f" Linked spec PR(s): {numbers}." if numbers else ""
        return (
            "I'm not starting implementation because the linked spec PR(s) "
            "have not been marked `plan-approved`."
            + suffix
        )
    updating = " (updating the existing draft PR)" if existing_implementation_pr else ""
    if spec_context_source == "approved-pr":
        return (
            "I'm implementing this issue on top of the approved spec PR's branch"
            + updating
            + "."
        )
    if spec_context_source == "directory":
        return (
            "I'm implementing this issue using the repository's directory specs"
            + updating
            + "."
        )
    return (
        "I'm implementing this issue with no spec context"
        + updating
        + "."
    )


def format_implementation_complete_line(
    *,
    updated_spec_pr: bool,
    existing_implementation_pr: bool,
    pr_url: str,
) -> str:
    """State-aware completion line for the implementation workflow."""
    if updated_spec_pr:
        return (
            f"I pushed implementation updates to the linked approved [spec PR]({pr_url})."
        )
    if existing_implementation_pr:
        return (
            f"I updated the existing draft [implementation PR]({pr_url}) for this issue."
        )
    return f"I created a new draft [implementation PR]({pr_url}) for this issue."


def format_review_start_line(
    *, spec_only: bool, is_rereview: bool
) -> str:
    """State-aware opening line for the review-pull-request workflow."""
    kind = "spec-only pull request" if spec_only else "pull request"
    if is_rereview:
        return f"I'm re-reviewing this {kind} in response to a review request."
    return f"I'm starting a first review of this {kind}."


def format_pr_comment_start_line(
    *, is_review_reply: bool, has_spec_context: bool, is_review_body: bool = False
) -> str:
    """State-aware opening line for the respond-to-pr-comment workflow."""
    if is_review_reply:
        source = "an inline review-thread comment"
    elif is_review_body:
        source = "a PR review body"
    else:
        source = "a PR conversation comment"
    spec_clause = (
        " Spec context was found and will be used to ground the change."
        if has_spec_context
        else ""
    )
    return (
        f"I'm working on changes requested in this PR (responding to {source})."
        + spec_clause
    )


def format_enforce_start_line(
    *, explicit_issue: bool, change_kind: str
) -> str:
    """State-aware opening line for the enforce-pr-issue-state workflow."""
    association = (
        "an explicitly linked issue"
        if explicit_issue
        else "a likely matching ready issue"
    )
    return (
        f"I'm checking this {change_kind} PR for association with {association}."
    )


def _workflow_run_url() -> str:
    """Build the GitHub Actions workflow run URL from environment variables."""
    server_url = optional_env("GITHUB_SERVER_URL") or "https://github.com"
    repository = optional_env("GITHUB_REPOSITORY")
    run_id = optional_env("GITHUB_RUN_ID")
    if not repository or not run_id:
        return ""
    return f"{server_url}/{repository}/actions/runs/{run_id}"


def _format_progress_link_section(session_link: str) -> str:
    normalized_link = session_link.strip()
    if "/conversation/" in normalized_link:
        return f"You can view [the conversation on Warp]({normalized_link})."
    return f"You can follow along in [the session on Warp]({normalized_link})."


def _format_triage_session_link(session_link: str) -> str:
    """Format a session link as a markdown link for the triage workflow."""
    normalized_link = session_link.strip()
    return f"[the triage session on Warp]({normalized_link})"


def append_comment_sections(existing_body: str, metadata: str, sections: list[str]) -> str:
    content, metadata = split_comment_body(existing_body, metadata)
    normalized_sections = [section.strip() for section in sections if section and section.strip()]
    if not content:
        return build_comment_body("\n\n".join(normalized_sections), metadata)
    updated_sections = [section.strip() for section in content.split("\n\n") if section.strip()]
    # Drop any prior "Powered by" suffix so ``build_comment_body`` can
    # re-add it as the last section after new sections are appended.
    updated_sections = [s for s in updated_sections if s != POWERED_BY_SUFFIX]
    for section in normalized_sections:
        if section.startswith(_PROGRESS_LINK_PREFIXES):
            updated_sections = [
                existing_section
                for existing_section in updated_sections
                if not existing_section.startswith(_PROGRESS_LINK_PREFIXES)
            ]
            updated_sections.append(section)
            continue
        if section not in updated_sections:
            updated_sections.append(section)
    return build_comment_body("\n\n".join(updated_sections), metadata)


def resolve_oz_assigner_login(
    github: Repository,
    owner: str,
    repo: str,
    issue_number: int,
    *,
    event_payload: dict[str, Any],
) -> str:
    if (
        event_payload.get("action") == "assigned"
        and (event_payload.get("assignee") or {}).get("login") == "oz-agent"
    ):
        return (event_payload.get("sender") or {}).get("login") or ""

    events = list(github.get_issue(issue_number).get_events())
    matching_events = [
        event
        for event in events
        if get_field(event, "event") == "assigned"
        and get_login(get_field(event, "assignee")) == "oz-agent"
    ]
    if not matching_events:
        return (event_payload.get("sender") or {}).get("login") or ""

    matching_events.sort(
        key=lambda event: (
            get_field(event, "created_at").astimezone(timezone.utc)
            if isinstance(get_field(event, "created_at"), datetime)
            else parse_datetime(str(get_field(event, "created_at") or "1970-01-01T00:00:00Z"))
        ),
        reverse=True,
    )
    return get_login(get_field(matching_events[0], "actor"))


def resolve_progress_requester_login(
    github: Repository,
    owner: str,
    repo: str,
    issue_number: int,
    *,
    event_payload: dict[str, Any] | None = None,
    requester_login: str = "",
) -> str:
    normalized_requester = requester_login.strip().removeprefix("@")
    if normalized_requester:
        return normalized_requester
    payload = event_payload or {}
    comment = payload.get("comment")
    if isinstance(comment, dict):
        comment_author = (comment.get("user") or {}).get("login") or ""
        if comment_author:
            return comment_author
    sender_login = (payload.get("sender") or {}).get("login") or ""
    if sender_login:
        return sender_login
    return resolve_oz_assigner_login(
        github,
        owner,
        repo,
        issue_number,
        event_payload=payload,
    )


class WorkflowProgressComment:
    def __init__(
        self,
        github: Repository,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        workflow: str,
        event_payload: dict[str, Any] | None = None,
        requester_login: str = "",
        review_reply_target: tuple[PullRequest, int] | None = None,
        comment_id: int | None = None,
        run_id: str | None = None,
        oz_run_id: str = "",
        session_link: str = "",
    ) -> None:
        self.github = github
        self.owner = owner
        self.repo = repo
        self.issue_number = issue_number
        self.workflow = workflow
        self.event_payload = event_payload or {}
        self.requester_login = requester_login
        # The Vercel control plane persists ``run_id`` (and any Oz run
        # id captured mid-run) alongside the GitHub comment id so the
        # cron poller can reconstruct an instance that targets the
        # exact comment posted at dispatch time. When the caller does
        # not provide a ``run_id`` we fall back to a fresh uuid so the
        # legacy GHA path keeps generating run-scoped metadata.
        self.run_id = (run_id or "").strip() or uuid.uuid4().hex
        self.github_run_id = optional_env("GITHUB_RUN_ID")
        self.oz_run_id: str = (oz_run_id or "").strip()
        self.metadata = comment_metadata(
            workflow,
            issue_number,
            run_id=self.run_id,
            oz_run_id=self.oz_run_id,
            github_run_id=self.github_run_id,
        )
        self._workflow_prefix = _workflow_metadata_prefix(workflow, issue_number)
        app_slug = optional_env("GH_APP_SLUG")
        self._bot_login = f"{app_slug}[bot]" if app_slug else ""
        self.comment_id: int | None = (
            int(comment_id) if comment_id is not None and int(comment_id) > 0 else None
        )
        self.session_link: str = (session_link or "").strip()
        # When set, progress updates are posted/edited as review-comment replies
        # within the triggering review thread instead of as PR-level issue
        # comments. The tuple is (pull_request, trigger_review_comment_id).
        self.review_reply_target = review_reply_target

    def start(self, status_line: str) -> None:
        self._append_sections([status_line])

    def record_session_link(self, session_link: str) -> None:
        normalized = session_link.strip()
        if not normalized:
            return
        if normalized == self.session_link:
            # The session link hasn't changed since the last successful
            # update, so there is nothing new to post.
            return
        try:
            self._append_sections([_format_progress_link_section(normalized)])
        except Exception:
            # Recording the session link happens from the run-agent poll
            # loop. A transient GitHub API failure here should not abort
            # the entire workflow run; try again on the next poll.
            return
        self.session_link = normalized

    def record_oz_run_id(self, oz_run_id: str) -> None:
        """Record the Oz agent run id and refresh the metadata marker.

        When the Oz run id becomes known mid-run (after ``client.agent.run``
        returns its run id), fold it into the comment metadata so the marker
        on the GitHub comment captures the Oz run id alongside the GitHub
        Actions run id.
        """
        normalized = (oz_run_id or "").strip()
        if not normalized or normalized == self.oz_run_id:
            return
        try:
            self.oz_run_id = normalized
            self.metadata = comment_metadata(
                self.workflow,
                self.issue_number,
                run_id=self.run_id,
                oz_run_id=self.oz_run_id,
                github_run_id=self.github_run_id,
            )
            existing = self._get_or_find_existing_comment()
            if existing is None:
                return
            existing_body = str(get_field(existing, "body") or "")
            content = _strip_workflow_metadata(existing_body, self._workflow_prefix)
            new_body = build_comment_body(content, self.metadata)
            if new_body == existing_body:
                return
            self._update_comment(int(get_field(existing, "id")), new_body)
        except Exception:
            # Refreshing the metadata marker is best-effort; a transient
            # GitHub API failure should not abort the workflow run.
            return

    def complete(self, status_line: str) -> None:
        self._append_sections([status_line])

    def report_error(self) -> None:
        """Update the progress comment to indicate a workflow failure.

        ``report_error`` is the last-chance hook before the caller re-raises,
        so it must not depend on GitHub API calls beyond the comment CRUD
        itself. The common reason this runs is that a prior GitHub API call
        failed (outage, rate limit, etc.), so we reuse the requester login
        cached by earlier successful comment updates rather than re-resolving
        it via ``resolve_progress_requester_login`` (which can trigger an
        events API lookup through ``resolve_oz_assigner_login``). When the
        fallback comment write itself fails, surface the problem via logs
        and a GitHub Actions ``::error::`` annotation instead of silently
        swallowing the exception.
        """
        run_url = _workflow_run_url()
        if run_url:
            message = (
                "I ran into an unexpected error while working on this. "
                f"You can view [the workflow run]({run_url}) for more details."
            )
        else:
            message = "I ran into an unexpected error while working on this."
        sections: list[str] = []
        normalized_requester = self.requester_login.strip().removeprefix("@")
        if normalized_requester:
            sections.append(f"@{normalized_requester}")
        sections.append(message)
        if self.session_link:
            sections.append(_format_progress_link_section(self.session_link))
        body = build_comment_body("\n\n".join(sections), self.metadata)
        try:
            existing = self._get_or_find_existing_comment()
            if existing is None:
                created = self._create_comment(body)
                self.comment_id = int(get_field(created, "id"))
                return
            self._update_comment(int(get_field(existing, "id")), body)
            self.comment_id = int(get_field(existing, "id"))
        except Exception:
            logger.exception(
                "Failed to post workflow error comment for %s/%s issue #%s",
                self.owner,
                self.repo,
                self.issue_number,
            )
            actions.error(
                f"Oz workflow '{self.workflow}' failed and the user-facing error "
                f"comment could not be posted to issue #{self.issue_number}. "
                f"See the workflow run logs for details."
            )

    def replace_body(self, content: str) -> None:
        """Replace the full comment body, preserving the metadata marker."""
        requester = resolve_progress_requester_login(
            self.github,
            self.owner,
            self.repo,
            self.issue_number,
            event_payload=self.event_payload,
            requester_login=self.requester_login,
        )
        self._cache_requester_login(requester)
        sections: list[str] = []
        if requester:
            sections.append(f"@{requester}")
        sections.append(content)
        body = build_comment_body("\n\n".join(sections), self.metadata)
        existing = self._get_or_find_existing_comment()
        if existing is None:
            created = self._create_comment(body)
            self.comment_id = int(get_field(created, "id"))
            return
        self._update_comment(int(get_field(existing, "id")), body)
        self.comment_id = int(get_field(existing, "id"))

    def cleanup(self) -> None:
        """Delete the progress comment if one exists from this or a previous run."""
        if self.comment_id is not None:
            try:
                self._delete_comment(self.comment_id)
            except Exception:
                pass
            self.comment_id = None
            return
        while True:
            existing = self._find_any_workflow_comment()
            if existing is None:
                break
            try:
                self._delete_comment(int(get_field(existing, "id")))
            except Exception:
                break
        self.comment_id = None

    def _cache_requester_login(self, requester: str) -> None:
        """Cache a resolved requester login so later calls can reuse it.

        ``report_error`` must not trigger new GitHub API calls to resolve
        the requester login when the workflow is already failing (e.g. a
        GitHub API outage). Caching the resolved value after every
        successful update lets the error path reuse it without needing
        another events/issue lookup.
        """
        normalized = (requester or "").strip().removeprefix("@")
        if normalized:
            self.requester_login = normalized

    def _append_sections(self, sections: list[str]) -> None:
        normalized_sections = [section.strip() for section in sections if section and section.strip()]
        requester = resolve_progress_requester_login(
            self.github,
            self.owner,
            self.repo,
            self.issue_number,
            event_payload=self.event_payload,
            requester_login=self.requester_login,
        )
        self._cache_requester_login(requester)
        if requester:
            normalized_sections.insert(0, f"@{requester}")
        if not normalized_sections:
            return
        existing = self._get_or_find_existing_comment()
        if existing is None:
            created = self._create_comment(
                build_comment_body("\n\n".join(normalized_sections), self.metadata),
            )
            created_id = int(get_field(created, "id"))
            self.comment_id = self._dedupe_duplicate_created_comments(created_id=created_id)
            return
        existing_body = str(get_field(existing, "body") or "")
        # Strip any workflow metadata marker already in the body before
        # re-appending with our own marker. This matters when we adopt a
        # same-run sibling comment, whose run-specific
        # marker would otherwise be treated as a body section and
        # duplicated alongside ours.
        existing_body = _strip_workflow_metadata(existing_body, self._workflow_prefix)
        updated_body = append_comment_sections(existing_body, self.metadata, normalized_sections)
        self._update_comment(int(get_field(existing, "id")), updated_body)
        self.comment_id = int(get_field(existing, "id"))

    def _comment_matches_current_run(self, comment: IssueComment | PullRequestComment) -> bool:
        if self._bot_login and get_login(get_field(comment, "user")).lower() != self._bot_login.lower():
            return False
        body = str(get_field(comment, "body") or "")
        if self._workflow_prefix not in body:
            return False
        current_github_run_id = self.github_run_id.strip()
        if not current_github_run_id:
            return True
        metadata = _parse_workflow_metadata(body, self._workflow_prefix) or {}
        return str(metadata.get("github_run_id") or "").strip() == current_github_run_id

    def _dedupe_duplicate_created_comments(self, *, created_id: int) -> int:
        """Consolidate progress comments for this workflow+issue and GitHub run.

        Two situations can leave duplicate progress comments behind:

        - PyGitHub's default retry policy retries POST requests on 5xx
          responses. When GitHub returns a 5xx but actually processed the
          create-comment request server-side, those retries produce
          duplicates that all share this run's unique ``run_id`` marker.
        - Multiple ``WorkflowProgressComment`` instances created during
          the same GitHub Actions run can both list comments, see no
          existing same-run match, and create their own comment before
          either learns of the other. The resulting comments share the
          stable workflow+issue prefix and the same ``github_run_id``.

        In both cases, gather every progress comment for this
        workflow+issue that belongs to the current GitHub Actions run,
        keep the oldest (lowest-numbered) as the canonical entry, and
        delete the rest. Return the id of the canonical comment so the
        caller can adopt it as its own ``comment_id``. Best-effort: if
        listing the comments fails, fall back to the just-created id.
        """
        try:
            comments = self._list_comments()
        except Exception:
            return created_id
        match_ids = sorted(
            int(get_field(comment, "id") or 0)
            for comment in comments
            if isinstance(get_field(comment, "body"), str)
            and self._comment_matches_current_run(comment)
        )
        match_ids = [cid for cid in match_ids if cid > 0]
        if not match_ids:
            return created_id
        canonical_id = match_ids[0]
        for comment_id in match_ids[1:]:
            try:
                self._delete_comment(comment_id)
            except Exception:
                # Deleting duplicates is best-effort; leave extras in
                # place rather than letting cleanup errors abort the
                # workflow.
                continue
        return canonical_id

    def _find_any_workflow_comment(self) -> IssueComment | PullRequestComment | None:
        """Find any progress comment for this workflow on this issue, regardless of run."""
        comments = self._list_comments()
        return next(
            (
                comment
                for comment in comments
                if isinstance(get_field(comment, "body"), str)
                and self._workflow_prefix in (get_field(comment, "body") or "")
                and (
                    not self._bot_login
                    or get_login(get_field(comment, "user")).lower() == self._bot_login.lower()
                )
            ),
            None,
        )

    def _get_or_find_existing_comment(self) -> IssueComment | PullRequestComment | None:
        if self.comment_id is not None:
            try:
                return self._get_comment(self.comment_id)
            except UnknownObjectException:
                self.comment_id = None
        # Reuse only comments that belong to this workflow+issue and the
        # current GitHub Actions run. A later run should create a fresh
        # progress comment rather than appending onto an earlier run's
        # history.
        comments = self._list_comments()
        matches = [
            comment
            for comment in comments
            if isinstance(get_field(comment, "body"), str)
            and self._comment_matches_current_run(comment)
        ]
        if not matches:
            return None
        # If multiple same-run matches already exist, adopt the oldest
        # so every instance created during this run picks the same
        # canonical entry.
        matches.sort(key=lambda comment: int(get_field(comment, "id") or 0))
        canonical = matches[0]
        self.comment_id = int(get_field(canonical, "id"))
        return canonical

    def _list_comments(self) -> list[IssueComment] | list[PullRequestComment]:
        """List candidate progress comments for the current scope."""
        if self.review_reply_target is not None:
            pr, trigger_comment_id = self.review_reply_target
            all_review_comments = list(pr.get_review_comments())
            return _filter_review_comments_in_thread(all_review_comments, trigger_comment_id)
        return list(self.github.get_issue(self.issue_number).get_comments())

    def _create_comment(self, body: str) -> IssueComment | PullRequestComment:
        if self.review_reply_target is not None:
            pr, trigger_comment_id = self.review_reply_target
            return pr.create_review_comment_reply(trigger_comment_id, body)
        return self.github.get_issue(self.issue_number).create_comment(body)

    def _get_comment(self, comment_id: int) -> IssueComment | PullRequestComment:
        if self.review_reply_target is not None:
            pr, _ = self.review_reply_target
            return pr.get_review_comment(comment_id)
        return self.github.get_issue(self.issue_number).get_comment(comment_id)

    def _update_comment(self, comment_id: int, body: str) -> IssueComment | PullRequestComment:
        if self.review_reply_target is not None:
            pr, _ = self.review_reply_target
            review_comment = pr.get_review_comment(comment_id)
            review_comment.edit(body)
            return review_comment
        issue_comment = self.github.get_issue(self.issue_number).get_comment(comment_id)
        issue_comment.edit(body)
        return issue_comment

    def _delete_comment(self, comment_id: int) -> None:
        if self.review_reply_target is not None:
            pr, _ = self.review_reply_target
            pr.get_review_comment(comment_id).delete()
            return
        self.github.get_issue(self.issue_number).get_comment(comment_id).delete()


def record_run_session_link(progress: WorkflowProgressComment, run: RunItem) -> None:
    """Record the current Oz session link and run id on a progress comment when available."""
    oz_run_id = getattr(run, "run_id", None) or ""
    if oz_run_id:
        progress.record_oz_run_id(str(oz_run_id))
    session_link = getattr(run, "session_link", None) or ""
    progress.record_session_link(session_link)


# Maps issue label names to conventional commit type prefixes.
_LABEL_TO_COMMIT_TYPE: dict[str, str] = {
    "bug": "fix",
    "enhancement": "feat",
    "feature": "feat",
    "documentation": "docs",
    "refactor": "refactor",
    "chore": "chore",
    "performance": "perf",
    "test": "test",
    "ci": "ci",
}


def conventional_commit_prefix(labels: list[Any], *, default: str = "feat") -> str:
    """Derive a conventional-commit type prefix from issue labels.

    Returns the first matching prefix found by scanning *labels* against a
    known mapping, or *default* when no label matches.
    """
    for label in labels:
        name = get_label_name(label).lower()
        if name in _LABEL_TO_COMMIT_TYPE:
            return _LABEL_TO_COMMIT_TYPE[name]
    return default


# Accounts created on or after this date use the ``ID+login`` noreply format.
# See https://docs.github.com/en/account-and-profile/reference/email-addresses-reference#your-noreply-email-address
_NOREPLY_ID_CUTOFF = datetime(2017, 7, 18, tzinfo=timezone.utc)
_OZ_COMMIT_AUTHOR_NAME = "Oz"
_OZ_COMMIT_AUTHOR_EMAIL = "oz-agent@warp.dev"


def _noreply_email(login: str, user_id: int | None, created_at: datetime | str | None) -> str:
    """Build the GitHub noreply email for *login*."""
    if created_at is not None and user_id is not None:
        try:
            parsed_created_at = (
                created_at.astimezone(timezone.utc)
                if isinstance(created_at, datetime)
                else parse_datetime(created_at)
            )
            if parsed_created_at >= _NOREPLY_ID_CUTOFF:
                return f"{user_id}+{login}@users.noreply.github.com"
        except (ValueError, TypeError):
            pass
    return f"{login}@users.noreply.github.com"


def resolve_coauthor_line(
    github: Github,
    event_payload: dict[str, Any],
) -> str:
    """Resolve a ``Co-Authored-By`` line from the event that triggered the workflow."""
    comment = event_payload.get("comment")
    login: str = ""
    if isinstance(comment, dict):
        login = (comment.get("user") or {}).get("login") or ""
    if not login:
        login = (event_payload.get("sender") or {}).get("login") or ""
    if not login:
        return ""

    try:
        user = github.get_user(login)
    except Exception:
        user = None

    name = (get_field(user, "name") if user else None) or login
    user_id = get_field(user, "id") if user else None
    created_at = get_field(user, "created_at") if user else None
    email = _noreply_email(login, user_id, created_at)
    return f"Co-Authored-By: {name} <{email}>"


def coauthor_prompt_lines(coauthor_line: str) -> str:
    """Return prompt directive lines for commit attribution."""
    lines = [
        f"- Before creating any commit, configure the local git author and committer as `{_OZ_COMMIT_AUTHOR_NAME} <{_OZ_COMMIT_AUTHOR_EMAIL}>`.",
        f"- Run `git config user.name \"{_OZ_COMMIT_AUTHOR_NAME}\"` and `git config user.email \"{_OZ_COMMIT_AUTHOR_EMAIL}\"` before committing.",
        "- Do not derive the git author or committer from the triggering issue, PR, comment, sender, or authenticated GitHub user.",
        "- Do not include issue number references (e.g. `(#N)`, `Refs #N`) in commit messages. The issue is already linked in the PR.",
    ]
    if coauthor_line:
        lines.extend(
            [
                f"- Include the following co-author attribution at the end of every commit message: {coauthor_line}",
                "- Do not attempt to resolve the co-author identity yourself (e.g. via GET /user). Use exactly the line provided above.",
            ]
        )
    else:
        lines.append("- Do not include any Co-Authored-By lines in commit messages.")
    return "\n".join(lines)

def spec_directory_name(issue_number: int) -> str:
    return f"GH{issue_number}"


def spec_directory_path(issue_number: int) -> str:
    return f"specs/{spec_directory_name(issue_number)}"


def build_spec_preview_section(owner: str, repo: str, branch_name: str, issue_number: int) -> str:
    spec_dir = spec_directory_path(issue_number)
    product_path = f"{spec_dir}/product.md"
    tech_path = f"{spec_dir}/tech.md"
    product_url = f"https://github.com/{owner}/{repo}/blob/{branch_name}/{product_path}"
    tech_url = f"https://github.com/{owner}/{repo}/blob/{branch_name}/{tech_path}"
    return (
        f"Preview generated specs:\n"
        f"- Product spec: [{product_path}]({product_url})\n"
        f"- Tech spec: [{tech_path}]({tech_url})"
    )


def _summarize_commits(commits: list[Any]) -> str:
    """Build a bulleted summary from a list of GitHub commit objects."""
    lines: list[str] = []
    max_lines = 15
    for commit in commits:
        if isinstance(commit, dict):
            msg = (get_field(commit, "commit") or {}).get("message") or ""
        else:
            msg = getattr(get_field(commit, "commit"), "message", "") or ""
        first_line = msg.split("\n", 1)[0].strip()
        if not first_line:
            continue
        if first_line.startswith("Merge "):
            continue
        lines.append(f"- {first_line}")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"- … and {len(lines) - max_lines} more commits"]
    return "\n".join(lines)


def build_pr_body(
    github: Repository,
    owner: str,
    repo: str,
    *,
    issue_number: int,
    head: str,
    base: str,
    session_link: str = "",
    closing_keyword: str = "Closes",
) -> str:
    """Build a descriptive PR body with an optional GitHub closing keyword."""
    sections: list[str] = []

    if closing_keyword:
        sections.append(f"{closing_keyword} #{issue_number}")
    else:
        sections.append(f"Related issue: #{issue_number}")

    commits: list[Any] = []
    try:
        comparison = github.compare(base, head)
    except UnknownObjectException:
        comparison = None
    if comparison is not None:
        commits = list(getattr(comparison, "commits", []) or [])
    summary = _summarize_commits(commits)
    if summary:
        sections.append(f"## Changes\n{summary}")

    if session_link:
        sections.append(f"Session: [view on Warp]({session_link})")

    sections.append(POWERED_BY_SUFFIX)
    return "\n\n".join(sections)


def build_next_steps_section(steps: list[str]) -> str:
    normalized_steps = [step.strip() for step in steps if step and step.strip()]
    if not normalized_steps:
        return ""
    return "Next steps:\n" + "\n".join(f"- {step}" for step in normalized_steps)


def branch_exists(github: Repository, owner: str, repo: str, branch: str) -> bool:
    try:
        github.get_git_ref(f"heads/{branch}")
        return True
    except UnknownObjectException:
        return False


def branch_updated_since(
    github: Repository,
    owner: str,
    repo: str,
    branch: str,
    *,
    created_after: datetime,
) -> bool:
    try:
        branch_ref = github.get_branch(branch)
    except UnknownObjectException:
        return False
    commit = get_field(branch_ref, "commit")
    commit_data = get_field(commit, "commit")
    committer = get_field(commit_data, "committer")
    commit_date = get_field(committer, "date")
    if not isinstance(commit_date, datetime):
        return False
    return commit_date.astimezone(timezone.utc) >= created_after


def find_matching_spec_prs(
    github: Repository,
    owner: str,
    repo: str,
    issue_number: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expected_spec_branch = f"oz-agent/spec-issue-{issue_number}"
    matching = list(github.get_pulls(state="all", head=f"{owner}:{expected_spec_branch}"))
    approved: list[dict[str, Any]] = []
    unapproved: list[dict[str, Any]] = []
    for pr in matching:
        # Use ``pr.labels`` directly instead of ``pr.as_issue().labels``.
        # ``PullRequest.as_issue()`` issues a fresh ``GET /issues/{n}`` call for
        # every PR, whereas labels are already attached to the ``PullRequest``
        # object returned by ``get_pulls``.
        labels = [get_label_name(label) for label in pr.labels]
        files = list(pr.get_files())
        spec_files = [
            str(file.filename)
            for file in files
            if str(file.filename).startswith("specs/")
        ]
        entry = {
            "number": pr.number,
            "url": pr.html_url,
            "updated_at": get_timestamp_text(pr.updated_at),
            "head_ref_name": pr.head.ref,
            "head_repo_full_name": pr.head.repo.full_name if pr.head.repo else "",
            "spec_files": spec_files,
        }
        if "plan-approved" in labels:
            approved.append(entry)
        else:
            unapproved.append(entry)
    approved.sort(key=lambda item: parse_datetime(item["updated_at"]), reverse=True)
    unapproved.sort(key=lambda item: parse_datetime(item["updated_at"]), reverse=True)
    return approved, unapproved


def read_local_spec_files(workspace: Path, issue_number: int) -> list[tuple[str, str]]:
    spec_dir_name = spec_directory_name(issue_number)
    spec_dir = workspace / "specs" / spec_dir_name
    results: list[tuple[str, str]] = []
    for name in ("product.md", "tech.md"):
        path = spec_dir / name
        if path.exists():
            rel = f"specs/{spec_dir_name}/{name}"
            results.append((rel, path.read_text(encoding="utf-8").strip()))
    return results


def resolve_spec_context_for_issue(
    github: Repository,
    owner: str,
    repo: str,
    issue_number: int,
    *,
    workspace: Path,
) -> dict[str, Any]:
    approved, unapproved = find_matching_spec_prs(github, owner, repo, issue_number)
    selected = approved[0] if approved else None
    local_specs = read_local_spec_files(workspace, issue_number)
    if selected and selected["head_repo_full_name"] != f"{owner}/{repo}":
        raise RuntimeError(
            f"Linked approved spec PR #{selected['number']} uses branch "
            f"{selected['head_repo_full_name']}:{selected['head_ref_name']}, which this workflow cannot push to."
        )

    spec_context_source = "approved-pr" if selected else "directory" if local_specs else ""
    spec_entries: list[dict[str, str]] = []
    if selected:
        for path in selected["spec_files"]:
            try:
                content_file = github.get_contents(path, ref=selected["head_ref_name"])
            except UnknownObjectException:
                continue
            if isinstance(content_file, list):
                continue
            spec_entries.append(
                {
                    "path": path,
                    "content": content_file.decoded_content.decode("utf-8").strip(),
                }
            )
    elif local_specs:
        for path, content in local_specs:
            spec_entries.append({"path": path, "content": content})

    return {
        "selected_spec_pr": selected,
        "approved_spec_prs": approved,
        "unapproved_spec_prs": unapproved,
        "spec_context_source": spec_context_source,
        "spec_entries": spec_entries,
    }


def _is_org_member(comment: Any) -> bool:
    return get_field(comment, "author_association") in ORG_MEMBER_ASSOCIATIONS


def _format_review_comment(comment: Any) -> str:
    login = get_login(get_field(comment, "user")) or "unknown"
    created = get_timestamp_text(get_field(comment, "created_at"))
    body = get_field(comment, "body") or ""
    path = get_field(comment, "path") or ""
    comment_id = get_field(comment, "id")
    id_prefix = f"[id={comment_id}] " if comment_id else ""
    prefix = f"{path}: " if path else ""
    return f"- {id_prefix}{prefix}{login} ({created}): {body}"


def review_thread_comments_text(
    all_review_comments: list[Any],
    trigger_comment_id: int,
) -> str:
    """Extract and format the review thread containing *trigger_comment_id*."""
    thread = _filter_review_comments_in_thread(all_review_comments, trigger_comment_id)
    filtered = [c for c in thread if _is_org_member(c)]
    if not filtered:
        return ""
    return "\n".join(_format_review_comment(c) for c in filtered)


def all_review_comments_text(review_comments: list[Any]) -> str:
    """Format all review comments grouped by file path, filtered to org members."""
    filtered = [c for c in review_comments if _is_org_member(c)]
    if not filtered:
        return ""

    by_path: dict[str, list[Any]] = {}
    for c in filtered:
        path = get_field(c, "path") or "(no file)"
        by_path.setdefault(path, []).append(c)

    sections: list[str] = []
    for path, comments in by_path.items():
        lines = [f"File: {path}"]
        for c in comments:
            login = get_login(get_field(c, "user")) or "unknown"
            created = get_timestamp_text(get_field(c, "created_at"))
            body = get_field(c, "body") or ""
            comment_id = get_field(c, "id")
            id_prefix = f"[id={comment_id}] " if comment_id else ""
            lines.append(f"  - {id_prefix}{login} ({created}): {body}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _resolve_review_thread_ids_for_comments(
    github_client: Github,
    owner: str,
    repo: str,
    pr_number: int,
    comment_ids: list[int],
) -> dict[int, str]:
    """Map review comment REST ids to their GraphQL review thread node ids.

    GitHub only exposes ``resolveReviewThread`` via GraphQL, and the REST
    review-comment id (``databaseId`` in GraphQL) is not itself a thread id.
    We walk the PR's review threads and record the thread node id for each
    comment we care about. Threads without any matching comment are ignored.
    """
    if not comment_ids:
        return {}
    wanted: set[int] = {int(cid) for cid in comment_ids}
    query = (
        "query($owner: String!, $name: String!, $number: Int!, $after: String) {"
        " repository(owner: $owner, name: $name) {"
        " pullRequest(number: $number) {"
        " reviewThreads(first: 100, after: $after) {"
        " pageInfo { hasNextPage endCursor }"
        " nodes {"
        " id isResolved"
        " comments(first: 100) { nodes { databaseId } }"
        " } } } } }"
    )
    mapping: dict[int, str] = {}
    cursor: str | None = None
    requester = github_client.requester
    while True:
        variables = {
            "owner": owner,
            "name": repo,
            "number": int(pr_number),
            "after": cursor,
        }
        _headers, data = requester.graphql_query(query, variables)
        repository = (data.get("data") or {}).get("repository") or {}
        pr_data = repository.get("pullRequest") or {}
        review_threads = pr_data.get("reviewThreads") or {}
        for thread in review_threads.get("nodes") or []:
            thread_id = thread.get("id")
            if not thread_id:
                continue
            thread_comments = (thread.get("comments") or {}).get("nodes") or []
            for comment_node in thread_comments:
                db_id = comment_node.get("databaseId")
                if isinstance(db_id, int) and db_id in wanted:
                    mapping[db_id] = thread_id
                    # A thread can satisfy multiple requested ids; don't break here.
        page_info = review_threads.get("pageInfo") or {}
        if not page_info.get("hasNextPage") or set(mapping.keys()) >= wanted:
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break
    return mapping


def _resolve_review_thread(github_client: Github, thread_id: str) -> bool:
    """Mark a single review thread as resolved via GraphQL.

    Returns ``True`` on success and ``False`` on any failure so callers can
    treat resolution as best-effort.
    """
    try:
        requester = github_client.requester
        requester.graphql_named_mutation(
            "resolveReviewThread",
            {"threadId": thread_id},
            "thread { id isResolved }",
        )
        return True
    except Exception:
        logger.exception(
            "Failed to resolve review thread %s via GraphQL", thread_id
        )
        return False


def post_resolved_review_comment_replies(
    github_client: Github,
    owner: str,
    repo: str,
    pr: PullRequest,
    resolved: list[ResolvedReviewComment],
) -> list[dict[str, Any]]:
    """Post replies and resolve review threads for agent-reported fixes.

    For each entry in *resolved* (``{"comment_id": int, "summary": str}``),
    post a reply on the original review thread explaining how the comment
    was addressed and, when possible, mark the thread as resolved via
    GraphQL. Failures for a single comment are logged and skipped so one
    bad reference does not abort the broader workflow.

    Returns a list of per-entry result records describing what happened so
    callers can surface them in progress comments or logs.
    """
    if not resolved:
        return []
    comment_ids = [int(entry["comment_id"]) for entry in resolved]
    try:
        thread_ids = _resolve_review_thread_ids_for_comments(
            github_client, owner, repo, pr.number, comment_ids
        )
    except Exception:
        logger.exception(
            "Failed to resolve review thread ids for PR #%s; replies will be posted without thread resolution",
            pr.number,
        )
        thread_ids = {}
    results: list[dict[str, Any]] = []
    for entry in resolved:
        comment_id = int(entry["comment_id"])
        summary = str(entry.get("summary") or "").strip()
        reply_posted = False
        thread_resolved = False
        reply_body = (
            f"Oz addressed this review comment as part of the current run.\n\n{summary}"
            if summary
            else "Oz addressed this review comment as part of the current run."
        )
        try:
            pr.create_review_comment_reply(comment_id, reply_body)
            reply_posted = True
        except Exception:
            logger.exception(
                "Failed to post reply on review comment %s for PR #%s",
                comment_id,
                pr.number,
            )
        thread_id = thread_ids.get(comment_id)
        # Skip thread resolution when the reply itself failed so we don't
        # silently close a review thread whose "resolved" status the
        # reviewer can no longer match up to a visible explanation.
        if thread_id and reply_posted:
            thread_resolved = _resolve_review_thread(github_client, thread_id)
        results.append(
            {
                "comment_id": comment_id,
                "thread_id": thread_id or "",
                "reply_posted": reply_posted,
                "thread_resolved": thread_resolved,
            }
        )
    return results


def _dedupe_ints(values: list[int]) -> list[int]:
    return list(dict.fromkeys(int(value) for value in values))


def _pull_request_head_ref(pr: Any) -> str:
    return str(get_field(get_field(pr, "head"), "ref") or "")


def _deterministic_issue_candidates(pr: Any, changed_files: list[str]) -> list[int]:
    head_ref = _pull_request_head_ref(pr)
    branch_issue_matches = [
        int(match.group(1))
        for match in re.finditer(
            r"(?:^|/)(?:spec|implement)-issue-(\d+)(?:$|[/-])",
            head_ref,
        )
    ]
    spec_file_issue_numbers = [
        int(match.group(1))
        for filename in changed_files
        for match in [re.match(r"^specs/GH(\d+)/(?:product|tech)\.md$", filename)]
        if match
    ]
    return _dedupe_ints(branch_issue_matches + spec_file_issue_numbers)


def _get_issue_from_cache(
    github: Repository,
    number: int,
    *,
    issue_cache: dict[int, Any] | None = None,
) -> Any:
    if issue_cache is not None and number in issue_cache:
        return issue_cache[number]
    issue = github.get_issue(number)
    if issue_cache is not None:
        issue_cache[number] = issue
    return issue


def _resolve_deterministic_issue_numbers(
    github: Repository,
    pr: Any,
    changed_files: list[str],
    *,
    issue_cache: dict[int, Any] | None = None,
) -> list[int]:
    resolved: list[int] = []
    for candidate in _deterministic_issue_candidates(pr, changed_files):
        try:
            issue = _get_issue_from_cache(github, candidate, issue_cache=issue_cache)
        except UnknownObjectException:
            continue
        if not issue.pull_request:
            resolved.append(candidate)
    return resolved


def _graphql_requester(source: Any) -> Any | None:
    req = getattr(source, "requester", None)
    return req if req is not None else getattr(source, "_requester", None)


def _normalize_github_linked_issue(node: Any, *, source: str) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None
    number = node.get("number")
    if not isinstance(number, int):
        return None
    repository = node.get("repository") or {}
    owner = ((repository.get("owner") or {}).get("login") or "").strip()
    repo = str(repository.get("name") or "").strip()
    if not owner or not repo:
        return None
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "source": source,
    }


def _graphql_pull_request_data(
    requester: Any,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    _headers, data = requester.graphql_query(query, variables)
    return (
        ((data.get("data") or {}).get("repository") or {}).get("pullRequest")
        or {}
    )


def _fetch_closing_issue_references(
    requester: Any,
    owner: str,
    repo: str,
    pr_number: int,
) -> list[dict[str, Any]]:
    linked: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    cursor: str | None = None
    while True:
        pr_data = _graphql_pull_request_data(
            requester,
            _CLOSING_ISSUES_QUERY,
            {
                "owner": owner,
                "name": repo,
                "number": int(pr_number),
                "after": cursor,
            },
        )
        closing_refs = pr_data.get("closingIssuesReferences") or {}
        for node in closing_refs.get("nodes") or []:
            issue_ref = _normalize_github_linked_issue(
                node,
                source="closingIssuesReferences",
            )
            if issue_ref is None:
                continue
            key = (
                issue_ref["owner"].lower(),
                issue_ref["repo"].lower(),
                int(issue_ref["number"]),
                str(issue_ref["source"]),
            )
            linked[key] = issue_ref
        page_info = closing_refs.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break
    return sorted(
        linked.values(),
        key=lambda item: (
            str(item["owner"]).lower(),
            str(item["repo"]).lower(),
            int(item["number"]),
            str(item["source"]),
        ),
    )


def _fetch_manual_linked_issue_references(
    requester: Any,
    owner: str,
    repo: str,
    pr_number: int,
) -> list[dict[str, Any]]:
    connected: dict[tuple[str, str, int], dict[str, Any]] = {}
    cursor: str | None = None
    while True:
        pr_data = _graphql_pull_request_data(
            requester,
            _MANUAL_LINKED_ISSUES_QUERY,
            {
                "owner": owner,
                "name": repo,
                "number": int(pr_number),
                "after": cursor,
            },
        )
        timeline_items = pr_data.get("timelineItems") or {}
        for node in timeline_items.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            issue_ref = _normalize_github_linked_issue(
                node.get("subject"),
                source="manualLink",
            )
            if issue_ref is None:
                continue
            key = (
                issue_ref["owner"].lower(),
                issue_ref["repo"].lower(),
                int(issue_ref["number"]),
            )
            typename = str(node.get("__typename") or "")
            if typename == "ConnectedEvent":
                connected[key] = issue_ref
            elif typename == "DisconnectedEvent":
                connected.pop(key, None)
        page_info = timeline_items.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break
    return sorted(
        connected.values(),
        key=lambda item: (
            str(item["owner"]).lower(),
            str(item["repo"]).lower(),
            int(item["number"]),
            str(item["source"]),
        ),
    )


def _fetch_github_linked_issues_for_pr(
    github: Repository,
    owner: str,
    repo: str,
    pr: Any,
) -> list[dict[str, Any]]:
    requester = _graphql_requester(github)
    pr_number = get_field(pr, "number")
    if requester is None or not isinstance(pr_number, int):
        return []
    try:
        merged: dict[tuple[str, str, int], dict[str, Any]] = {}
        for issue_ref in _fetch_closing_issue_references(
            requester, owner, repo, pr_number
        ):
            key = (
                str(issue_ref["owner"]).lower(),
                str(issue_ref["repo"]).lower(),
                int(issue_ref["number"]),
            )
            merged[key] = issue_ref
        for issue_ref in _fetch_manual_linked_issue_references(
            requester, owner, repo, pr_number
        ):
            key = (
                str(issue_ref["owner"]).lower(),
                str(issue_ref["repo"]).lower(),
                int(issue_ref["number"]),
            )
            if key not in merged:
                merged[key] = issue_ref
        return sorted(
            merged.values(),
            key=lambda item: (
                str(item["owner"]).lower(),
                str(item["repo"]).lower(),
                int(item["number"]),
            ),
        )
    except Exception:
        logger.exception(
            "Failed to fetch linked issue data for PR #%s in %s/%s",
            pr_number,
            owner,
            repo,
        )
        return []


def _same_repo_issue_numbers(
    owner: str,
    repo: str,
    issue_refs: list[dict[str, Any]],
) -> list[int]:
    normalized_owner = owner.lower()
    normalized_repo = repo.lower()
    return _dedupe_ints(
        [
            int(issue_ref["number"])
            for issue_ref in issue_refs
            if str(issue_ref.get("owner") or "").lower() == normalized_owner
            and str(issue_ref.get("repo") or "").lower() == normalized_repo
        ]
    )


def resolve_pr_association(
    github: Repository,
    owner: str,
    repo: str,
    pr: Any,
    changed_files: list[str],
    *,
    issue_cache: dict[int, Any] | None = None,
) -> dict[str, Any]:
    deterministic_issue_numbers = _resolve_deterministic_issue_numbers(
        github,
        pr,
        changed_files,
        issue_cache=issue_cache,
    )
    github_linked_issues = _fetch_github_linked_issues_for_pr(github, owner, repo, pr)
    same_repo_linked_numbers = _same_repo_issue_numbers(
        owner,
        repo,
        github_linked_issues,
    )
    same_repo_issue_numbers = _dedupe_ints(
        deterministic_issue_numbers + same_repo_linked_numbers
    )

    primary_issue_number: int | None = None
    ambiguous = False
    if len(deterministic_issue_numbers) == 1:
        primary_issue_number = deterministic_issue_numbers[0]
    elif len(deterministic_issue_numbers) > 1:
        ambiguous = True
    elif len(same_repo_linked_numbers) == 1:
        primary_issue_number = same_repo_linked_numbers[0]
    elif len(same_repo_linked_numbers) > 1:
        ambiguous = True

    return {
        "deterministic_issue_numbers": deterministic_issue_numbers,
        "github_linked_issues": github_linked_issues,
        "same_repo_issue_numbers": same_repo_issue_numbers,
        "primary_issue_number": primary_issue_number,
        "ambiguous": ambiguous,
    }


def resolve_issue_number_for_pr(
    github: Repository,
    owner: str,
    repo: str,
    pr: Any,
    changed_files: list[str],
    *,
    issue_cache: dict[int, Any] | None = None,
) -> int | None:
    association = resolve_pr_association(
        github,
        owner,
        repo,
        pr,
        changed_files,
        issue_cache=issue_cache,
    )
    primary_issue_number = association.get("primary_issue_number")
    return int(primary_issue_number) if isinstance(primary_issue_number, int) else None


def is_spec_only_pr(changed_files: list[str]) -> bool:
    """Return True when every changed file lives under ``specs/``."""
    return bool(changed_files) and all(
        filename.startswith("specs/") for filename in changed_files
    )


def resolve_spec_context_for_pr(
    github: Repository,
    owner: str,
    repo: str,
    pr: Any,
    *,
    workspace: Path,
) -> dict[str, Any]:
    files = list(pr.get_files())
    changed_files = [str(file.filename) for file in files]
    issue_number = resolve_issue_number_for_pr(github, owner, repo, pr, changed_files)
    if not issue_number:
        return {
            "issue_number": None,
            "spec_context_source": "",
            "selected_spec_pr": None,
            "spec_entries": [],
            "changed_files": changed_files,
            "pr_files": files,
        }
    spec_context = resolve_spec_context_for_issue(
        github,
        owner,
        repo,
        issue_number,
        workspace=workspace,
    )
    spec_context["issue_number"] = issue_number
    spec_context["changed_files"] = changed_files
    spec_context["pr_files"] = files
    return spec_context
