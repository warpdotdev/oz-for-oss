"""Concrete prompt builders for the cloud-agent workflows.

The webhook handler routes an incoming webhook delivery to a workflow
name (see :mod:`lib.routing`) and then asks the dispatcher to evaluate
the route against the registry returned by :func:`build_builder_registry`.
Each registered builder takes the parsed webhook payload (and a fresh
:class:`Github` client) and returns a :class:`DispatchRequest` that the
dispatcher hands to the Oz SDK.

The builders are intentionally thin wrappers around the
``gather_*_context`` / ``build_*_prompt`` helpers that live alongside the
GitHub Actions entrypoints. Reusing those helpers keeps the cloud-mode
prompt and the GitHub-state mutations (in :mod:`lib.handlers`)
byte-for-byte identical with the legacy GitHub Actions paths.

Each builder also drives the :class:`WorkflowProgressComment` lifecycle:
it posts the workflow-specific "starting..." comment on the originating
issue/PR before the run is dispatched and stashes the resulting
``progress_comment_id`` (plus ``progress_run_id`` so the metadata
marker stays stable) onto ``DispatchRequest.payload_subset``. The cron
poller (see :mod:`lib.handlers`) reconstructs a
:class:`WorkflowProgressComment` from those fields when it applies the
result or reports a failure so each progress update edits the same
comment that was posted at dispatch time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from github import Github

from .dispatch import DispatchRequest, PromptBuilder
from .routing import (
    OZ_REVIEW_COMMAND,
    OZ_VERIFY_COMMAND,
    WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
    WORKFLOW_CREATE_SPEC_FROM_ISSUE,
    WORKFLOW_ENFORCE_PR_ISSUE_STATE,
    WORKFLOW_RESPOND_TO_PR_COMMENT,
    WORKFLOW_REVIEW_PR,
    WORKFLOW_TRIAGE_NEW_ISSUES,
    WORKFLOW_VERIFY_PR_COMMENT,
)

logger = logging.getLogger(__name__)


def _start_progress_comment(
    *,
    repo_handle: Any,
    owner: str,
    repo: str,
    issue_number: int,
    workflow: str,
    start_line: str,
    requester_login: str,
    event_payload: Mapping[str, Any] | None = None,
    review_reply_target: tuple[Any, int] | None = None,
) -> tuple[int, str]:
    """Post the workflow-specific "starting..." progress comment.

    Returns a ``(progress_comment_id, progress_run_id)`` tuple the
    builder stuffs into ``payload_subset`` so the cron poller can
    reconstruct the same :class:`WorkflowProgressComment` instance when
    the run terminates. ``progress_comment_id`` is ``0`` if the start
    call could not produce an id (for example because the GitHub API
    refused the post). The cron-side reconstruction tolerates a missing
    id by falling back to the workflow-prefix comment lookup that
    :class:`WorkflowProgressComment` already implements.
    """
    from oz_workflows.helpers import (  # type: ignore[import-not-found]
        WorkflowProgressComment,
    )

    progress = WorkflowProgressComment(
        repo_handle,
        owner,
        repo,
        issue_number,
        workflow=workflow,
        event_payload=dict(event_payload or {}),
        requester_login=requester_login,
        review_reply_target=review_reply_target,
    )
    try:
        progress.start(start_line)
    except Exception:
        # Failing to post the starting comment must not abort the
        # dispatch — the cron poller's failure handler will surface
        # the error to the user instead. Return whatever ids the
        # constructor produced so the cron-side reconstruction can
        # still rebuild the metadata marker.
        logger.exception(
            "Failed to post workflow-progress start comment for %s on issue #%s in %s/%s",
            workflow,
            issue_number,
            owner,
            repo,
        )
    return int(progress.comment_id or 0), str(progress.run_id or "")


def _resolve_owner_repo(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    """Pick the ``(owner, repo, owner/repo)`` triple from a webhook payload."""
    repo_obj = payload.get("repository") or {}
    if not isinstance(repo_obj, dict):
        raise ValueError("payload.repository is missing or not an object")
    full_name = str(repo_obj.get("full_name") or "").strip()
    if "/" not in full_name:
        raise ValueError(
            f"payload.repository.full_name {full_name!r} is not an 'owner/repo' slug"
        )
    owner, repo = full_name.split("/", 1)
    return owner, repo, full_name


def _resolve_installation_id(payload: Mapping[str, Any]) -> int:
    installation = payload.get("installation") or {}
    if not isinstance(installation, dict):
        raise ValueError("payload.installation is missing or not an object")
    raw = installation.get("id")
    try:
        installation_id = int(raw or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"payload.installation.id is not an int: {raw!r}"
        ) from exc
    if installation_id <= 0:
        raise ValueError("payload.installation.id must be a positive integer")
    return installation_id


def _resolve_pr_number(payload: Mapping[str, Any]) -> int:
    pr = payload.get("pull_request")
    if isinstance(pr, dict) and pr.get("number") is not None:
        return int(pr["number"])
    issue = payload.get("issue")
    if isinstance(issue, dict) and issue.get("number") is not None:
        return int(issue["number"])
    raise ValueError("payload does not include a PR or issue number")


def _resolve_issue_number(payload: Mapping[str, Any]) -> int:
    """Pick the issue number from a webhook payload.

    Accepts payloads with ``issue.number`` (the ``issues`` and
    ``issue_comment`` event shapes the webhook routes to triage). Raises
    ``ValueError`` when neither field is present so the caller can
    surface a structured error rather than silently dispatching against
    issue ``0``.
    """
    issue = payload.get("issue")
    if isinstance(issue, dict) and issue.get("number") is not None:
        return int(issue["number"])
    raise ValueError("payload does not include an issue number")


def _resolve_requester(payload: Mapping[str, Any]) -> str:
    """Best-effort lookup of the human that triggered the webhook event."""
    comment = payload.get("comment")
    if isinstance(comment, dict):
        login = (comment.get("user") or {}).get("login")
        if isinstance(login, str) and login.strip():
            return login.strip()
    review = payload.get("review")
    if isinstance(review, dict):
        login = (review.get("user") or {}).get("login")
        if isinstance(login, str) and login.strip():
            return login.strip()
    sender = payload.get("sender")
    if isinstance(sender, dict):
        login = sender.get("login")
        if isinstance(login, str) and login.strip():
            return login.strip()
    return ""


def _resolve_trigger_source(payload: Mapping[str, Any], event_hint: str | None = None) -> str:
    """Pick the ``trigger_source`` string the legacy review prompt expects."""
    if event_hint:
        return event_hint
    if isinstance(payload.get("review"), dict):
        return "pull_request_review"
    if isinstance(payload.get("comment"), dict):
        if isinstance(payload.get("pull_request"), dict):
            return "pull_request_review_comment"
        return "issue_comment"
    if isinstance(payload.get("pull_request"), dict):
        return "pull_request"
    return ""


def _resolve_trigger_kind(payload: Mapping[str, Any]) -> str:
    """Map the webhook payload onto ``respond-to-pr-comment``'s trigger_kind."""
    if isinstance(payload.get("review"), dict):
        return "review_body"
    if isinstance(payload.get("comment"), dict) and isinstance(
        payload.get("pull_request"), dict
    ):
        return "review"
    return "conversation"


def _resolve_trigger_comment_id(payload: Mapping[str, Any]) -> int:
    review = payload.get("review")
    if isinstance(review, dict):
        return int(review.get("id") or 0)
    comment = payload.get("comment")
    if isinstance(comment, dict):
        return int(comment.get("id") or 0)
    return 0


def _resolve_review_reply_target(payload: Mapping[str, Any], pr: Any) -> tuple[Any, int] | None:
    if isinstance(payload.get("comment"), dict) and isinstance(
        payload.get("pull_request"), dict
    ):
        comment_id = int(payload["comment"].get("id") or 0)
        if comment_id > 0:
            return (pr, comment_id)
    return None


def build_review_request(
    payload: Mapping[str, Any],
    *,
    github_client: Github,
    workspace_path: Path | None = None,
) -> DispatchRequest:
    """Build the :class:`DispatchRequest` for a PR review run."""
    from oz_workflows.helpers import (  # type: ignore[import-not-found]
        format_review_start_line,
    )
    from scripts.review_pr import (  # type: ignore[import-not-found]
        build_review_prompt_for_dispatch,
        gather_review_context,
    )

    owner, repo, full_name = _resolve_owner_repo(payload)
    installation_id = _resolve_installation_id(payload)
    pr_number = _resolve_pr_number(payload)
    requester = _resolve_requester(payload)
    trigger_source = _resolve_trigger_source(payload)
    repo_handle = github_client.get_repo(full_name)
    context = gather_review_context(
        repo_handle,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        trigger_source=trigger_source,
        requester=requester,
        workspace_path=workspace_path or Path("/tmp"),
    )
    is_rereview = trigger_source in {
        "issue_comment",
        "pull_request_review_comment",
    }
    progress_comment_id, progress_run_id = _start_progress_comment(
        repo_handle=repo_handle,
        owner=owner,
        repo=repo,
        issue_number=pr_number,
        workflow=WORKFLOW_REVIEW_PR,
        start_line=format_review_start_line(
            spec_only=bool(context.get("spec_only")),
            is_rereview=is_rereview,
        ),
        requester_login=requester,
        event_payload=payload,
    )
    prompt = build_review_prompt_for_dispatch(context)
    payload_subset: dict[str, Any] = dict(context)
    payload_subset["progress_comment_id"] = progress_comment_id
    payload_subset["progress_run_id"] = progress_run_id
    return DispatchRequest(
        workflow=WORKFLOW_REVIEW_PR,
        repo=full_name,
        installation_id=installation_id,
        config_name=WORKFLOW_REVIEW_PR,
        title=f"PR review #{pr_number}",
        skill_name=context["skill_name"],
        prompt=prompt,
        payload_subset=payload_subset,
    )


def build_respond_request(
    payload: Mapping[str, Any],
    *,
    github_client: Github,
    workspace_path: Path | None = None,
) -> DispatchRequest:
    """Build the :class:`DispatchRequest` for a respond-to-pr-comment run."""
    from scripts.respond_to_pr_comment import (  # type: ignore[import-not-found]
        build_pr_comment_prompt,
        gather_pr_comment_context,
    )

    owner, repo, full_name = _resolve_owner_repo(payload)
    installation_id = _resolve_installation_id(payload)
    pr_number = _resolve_pr_number(payload)
    requester = _resolve_requester(payload)
    trigger_kind = _resolve_trigger_kind(payload)
    trigger_comment_id = _resolve_trigger_comment_id(payload)
    repo_handle = github_client.get_repo(full_name)
    pr = repo_handle.get_pull(pr_number)
    review_reply_target = _resolve_review_reply_target(payload, pr)
    context = gather_pr_comment_context(
        repo_handle,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        trigger_kind=trigger_kind,
        trigger_comment_id=trigger_comment_id,
        requester=requester,
        event=dict(payload),
        review_reply_target=review_reply_target,
        workspace_path=workspace_path or Path("/tmp"),
        client=github_client,
        pr=pr,
    )
    progress_comment_id, progress_run_id = _start_progress_comment(
        repo_handle=repo_handle,
        owner=owner,
        repo=repo,
        issue_number=pr_number,
        workflow=WORKFLOW_RESPOND_TO_PR_COMMENT,
        start_line=str(
            context.get("progress_start_line") or ""
        ),
        requester_login=requester,
        event_payload=payload,
        review_reply_target=review_reply_target,
    )
    prompt = build_pr_comment_prompt(context)
    payload_subset: dict[str, Any] = dict(context)
    payload_subset["progress_comment_id"] = progress_comment_id
    payload_subset["progress_run_id"] = progress_run_id
    return DispatchRequest(
        workflow=WORKFLOW_RESPOND_TO_PR_COMMENT,
        repo=full_name,
        installation_id=installation_id,
        config_name=WORKFLOW_RESPOND_TO_PR_COMMENT,
        title=f"Respond to PR comment #{pr_number}",
        skill_name="implement-issue",
        prompt=prompt,
        payload_subset=payload_subset,
    )


def build_verify_request(
    payload: Mapping[str, Any],
    *,
    github_client: Github,
    workspace_path: Path | None = None,
) -> DispatchRequest:
    """Build the :class:`DispatchRequest` for a /oz-verify run."""
    from scripts.verify_pr_comment import (  # type: ignore[import-not-found]
        build_verification_prompt,
        gather_verify_context,
    )

    owner, repo, full_name = _resolve_owner_repo(payload)
    installation_id = _resolve_installation_id(payload)
    pr_number = _resolve_pr_number(payload)
    requester = _resolve_requester(payload)
    trigger_comment_id = _resolve_trigger_comment_id(payload)
    repo_handle = github_client.get_repo(full_name)
    context = gather_verify_context(
        repo_handle,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        trigger_comment_id=trigger_comment_id,
        requester=requester,
        workspace_path=workspace_path or Path("/tmp"),
    )
    progress_comment_id, progress_run_id = _start_progress_comment(
        repo_handle=repo_handle,
        owner=owner,
        repo=repo,
        issue_number=pr_number,
        workflow=WORKFLOW_VERIFY_PR_COMMENT,
        start_line=(
            "I'm running `/oz-verify` for this pull request using the "
            "repository's verification-enabled skills."
        ),
        requester_login=requester,
        event_payload=payload,
    )
    prompt = build_verification_prompt(
        owner=context["owner"],
        repo=context["repo"],
        pr_number=context["pr_number"],
        base_branch=context["base_branch"],
        head_branch=context["head_branch"],
        trigger_comment_id=context["trigger_comment_id"],
        requester=context["requester"],
        verification_skills_text=context["verification_skills_text"],
    )
    payload_subset: dict[str, Any] = dict(context)
    payload_subset["progress_comment_id"] = progress_comment_id
    payload_subset["progress_run_id"] = progress_run_id
    return DispatchRequest(
        workflow=WORKFLOW_VERIFY_PR_COMMENT,
        repo=full_name,
        installation_id=installation_id,
        config_name=WORKFLOW_VERIFY_PR_COMMENT,
        title=f"Verify PR #{pr_number}",
        skill_name="verify-pr",
        prompt=prompt,
        payload_subset=payload_subset,
    )


def build_triage_request(
    payload: Mapping[str, Any],
    *,
    github_client: Github,
    workspace_path: Path | None = None,
) -> DispatchRequest:
    """Build the :class:`DispatchRequest` for a ``triage-new-issues`` run.

    Drives the same start-comment lifecycle the legacy GitHub Actions
    ``triage_new_issues.process_issue`` path does so the cron poller
    can resume editing the same comment when the run terminates.
    """
    from oz_workflows.helpers import (  # type: ignore[import-not-found]
        format_triage_start_line,
    )
    from scripts.triage_new_issues import (  # type: ignore[import-not-found]
        build_triage_prompt_for_dispatch,
        gather_triage_context,
    )

    owner, repo, full_name = _resolve_owner_repo(payload)
    installation_id = _resolve_installation_id(payload)
    issue_number = _resolve_issue_number(payload)
    requester = _resolve_requester(payload)
    repo_handle = github_client.get_repo(full_name)
    triggering_comment_id = _resolve_trigger_comment_id(payload)
    from oz_workflows.helpers import (  # type: ignore[import-not-found]
        triggering_comment_prompt_text,
    )

    triggering_comment_text = triggering_comment_prompt_text(dict(payload))
    context = gather_triage_context(
        repo_handle,
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        requester=requester,
        triggering_comment_id=triggering_comment_id,
        triggering_comment_text=triggering_comment_text,
    )
    progress_comment_id, progress_run_id = _start_progress_comment(
        repo_handle=repo_handle,
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        workflow=WORKFLOW_TRIAGE_NEW_ISSUES,
        start_line=format_triage_start_line(
            is_retriage=bool(context.get("is_retriage")),
        ),
        requester_login=requester,
        event_payload=payload,
    )
    prompt = build_triage_prompt_for_dispatch(context)
    payload_subset: dict[str, Any] = dict(context)
    payload_subset["progress_comment_id"] = progress_comment_id
    payload_subset["progress_run_id"] = progress_run_id
    return DispatchRequest(
        workflow=WORKFLOW_TRIAGE_NEW_ISSUES,
        repo=full_name,
        installation_id=installation_id,
        config_name=WORKFLOW_TRIAGE_NEW_ISSUES,
        title=f"Triage issue #{issue_number}",
        skill_name="triage-issue",
        prompt=prompt,
        payload_subset=payload_subset,
    )


def build_create_spec_request(
    payload: Mapping[str, Any],
    *,
    github_client: Github,
    workspace_path: Path | None = None,
) -> DispatchRequest:
    """Build the :class:`DispatchRequest` for a ``create-spec-from-issue`` run.

    Triggered when an ``@oz-agent`` mention lands on a plain issue
    that already carries the ``ready-to-spec`` label. The webhook
    handler resolves the issue, gathers the spec context, posts the
    "starting/updating spec PR" progress comment, and dispatches the
    cloud agent. The cron poller picks up the SUCCEEDED run and calls
    :func:`apply_create_spec_result`.
    """
    from scripts.create_spec_from_issue import (  # type: ignore[import-not-found]
        build_create_spec_prompt_for_dispatch,
        gather_create_spec_context,
        SPEC_DRIVEN_IMPLEMENTATION_SKILL,
    )

    owner, repo, full_name = _resolve_owner_repo(payload)
    installation_id = _resolve_installation_id(payload)
    issue_number = _resolve_issue_number(payload)
    requester = _resolve_requester(payload)
    triggering_comment_id = _resolve_trigger_comment_id(payload)
    repo_handle = github_client.get_repo(full_name)
    from oz_workflows.helpers import (  # type: ignore[import-not-found]
        triggering_comment_prompt_text,
    )

    triggering_comment_text = triggering_comment_prompt_text(dict(payload))
    context = gather_create_spec_context(
        repo_handle,
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        requester=requester,
        triggering_comment_id=triggering_comment_id,
        triggering_comment_text=triggering_comment_text,
        event_payload=dict(payload),
        github_client=github_client,
    )
    progress_comment_id, progress_run_id = _start_progress_comment(
        repo_handle=repo_handle,
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        workflow=WORKFLOW_CREATE_SPEC_FROM_ISSUE,
        start_line=str(context.get("progress_start_line") or ""),
        requester_login=requester,
        event_payload=payload,
    )
    prompt = build_create_spec_prompt_for_dispatch(context)
    payload_subset: dict[str, Any] = dict(context)
    payload_subset["progress_comment_id"] = progress_comment_id
    payload_subset["progress_run_id"] = progress_run_id
    return DispatchRequest(
        workflow=WORKFLOW_CREATE_SPEC_FROM_ISSUE,
        repo=full_name,
        installation_id=installation_id,
        config_name=WORKFLOW_CREATE_SPEC_FROM_ISSUE,
        title=f"Create specs for issue #{issue_number}",
        skill_name=SPEC_DRIVEN_IMPLEMENTATION_SKILL,
        prompt=prompt,
        payload_subset=payload_subset,
    )


def build_create_implementation_request(
    payload: Mapping[str, Any],
    *,
    github_client: Github,
    workspace_path: Path | None = None,
) -> DispatchRequest:
    """Build the :class:`DispatchRequest` for a ``create-implementation-from-issue`` run.

    Triggered when an ``@oz-agent`` mention lands on a plain issue
    that already carries the ``ready-to-implement`` label. The webhook
    handler resolves the spec context (linked approved spec PR, if
    any), posts the implementation progress comment, and dispatches
    the cloud agent. The cron poller picks up the SUCCEEDED run and
    calls :func:`apply_create_implementation_result`.
    """
    from scripts.create_implementation_from_issue import (  # type: ignore[import-not-found]
        IMPLEMENT_SPECS_SKILL,
        build_create_implementation_prompt_for_dispatch,
        gather_create_implementation_context,
    )

    owner, repo, full_name = _resolve_owner_repo(payload)
    installation_id = _resolve_installation_id(payload)
    issue_number = _resolve_issue_number(payload)
    requester = _resolve_requester(payload)
    repo_handle = github_client.get_repo(full_name)
    from oz_workflows.helpers import (  # type: ignore[import-not-found]
        triggering_comment_prompt_text,
    )

    triggering_comment_text = triggering_comment_prompt_text(dict(payload))
    context = gather_create_implementation_context(
        repo_handle,
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        requester=requester,
        triggering_comment_text=triggering_comment_text,
        event_payload=dict(payload),
        workspace_path=workspace_path or Path("/tmp"),
        github_client=github_client,
    )
    progress_comment_id, progress_run_id = _start_progress_comment(
        repo_handle=repo_handle,
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        workflow=WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
        start_line=str(context.get("progress_start_line") or ""),
        requester_login=requester,
        event_payload=payload,
    )
    prompt = build_create_implementation_prompt_for_dispatch(context)
    payload_subset: dict[str, Any] = dict(context)
    payload_subset["progress_comment_id"] = progress_comment_id
    payload_subset["progress_run_id"] = progress_run_id
    return DispatchRequest(
        workflow=WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
        repo=full_name,
        installation_id=installation_id,
        config_name=WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
        title=f"Implement issue #{issue_number}",
        skill_name=IMPLEMENT_SPECS_SKILL,
        prompt=prompt,
        payload_subset=payload_subset,
    )


def build_enforce_request(
    payload: Mapping[str, Any],
    *,
    github_client: Github,
    workspace_path: Path | None = None,
) -> DispatchRequest:
    """Build the cloud-mode :class:`DispatchRequest` for the enforce flow.

    Used only when :func:`scripts.enforce_pr_issue_state.enforce_pr_state_synchronously`
    returns a ``need-cloud-match`` decision. The webhook handler runs
    the synchronous decision first (no agent run needed for the trivial
    cases) and only invokes this builder when the cloud agent is the
    last resort. The synchronous helper already posts the workflow's
    "checking this PR for association" start line, so this builder only
    needs to capture the resulting comment id for the cron poller.
    """
    from oz_workflows.helpers import (  # type: ignore[import-not-found]
        WorkflowProgressComment,
    )
    from scripts.enforce_pr_issue_state import (  # type: ignore[import-not-found]
        EnforceContext,
        enforce_pr_state_synchronously,
        gather_enforce_context,
    )

    owner, repo, full_name = _resolve_owner_repo(payload)
    installation_id = _resolve_installation_id(payload)
    pr_number = _resolve_pr_number(payload)
    requester = _resolve_requester(payload)
    repo_handle = github_client.get_repo(full_name)
    progress = WorkflowProgressComment(
        repo_handle,
        owner,
        repo,
        pr_number,
        workflow=WORKFLOW_ENFORCE_PR_ISSUE_STATE,
        event_payload=dict(payload),
        requester_login=requester,
    )
    decision = enforce_pr_state_synchronously(
        repo_handle,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        requester=requester,
        progress=progress,
    )
    if decision.action != "need-cloud-match":
        raise RuntimeError(
            "build_enforce_request invoked for a non-need-cloud-match decision: "
            f"{decision.action!r}"
        )
    enforce_context: EnforceContext = decision.context  # type: ignore[assignment]
    if enforce_context is None:
        raise RuntimeError("need-cloud-match decision missing EnforceContext")
    prompt, _candidate_issues = gather_enforce_context(
        repo_handle, context=enforce_context
    )
    payload_subset: dict[str, Any] = dict(enforce_context)
    payload_subset["progress_comment_id"] = int(progress.comment_id or 0)
    payload_subset["progress_run_id"] = str(progress.run_id or "")
    return DispatchRequest(
        workflow=WORKFLOW_ENFORCE_PR_ISSUE_STATE,
        repo=full_name,
        installation_id=installation_id,
        config_name=WORKFLOW_ENFORCE_PR_ISSUE_STATE,
        title=f"Associate PR #{pr_number} with ready issue",
        skill_name=None,
        prompt=prompt,
        payload_subset=payload_subset,
    )


def build_builder_registry(
    *,
    github_client_factory,
    workspace_path: Path | None = None,
) -> Mapping[str, PromptBuilder]:
    """Return the registry of prompt builders keyed by workflow name.

    *github_client_factory* is a zero-arg callable that mints a fresh
    :class:`Github` client from a per-installation token. It is invoked
    once per dispatch so a single bad installation cannot poison the
    cached client.
    """

    def _wrap(builder):
        def _adapter(payload: Mapping[str, Any]) -> DispatchRequest:
            github_client = github_client_factory()
            return builder(
                payload,
                github_client=github_client,
                workspace_path=workspace_path,
            )

        return _adapter

    return {
        WORKFLOW_REVIEW_PR: _wrap(build_review_request),
        WORKFLOW_RESPOND_TO_PR_COMMENT: _wrap(build_respond_request),
        WORKFLOW_VERIFY_PR_COMMENT: _wrap(build_verify_request),
        # ``enforce-pr-issue-state`` is special-cased in the webhook
        # handler: the synchronous helper handles allow/close inline,
        # and only the ``need-cloud-match`` branch falls through to
        # this builder. Registering it here keeps the dispatch path
        # uniform.
        WORKFLOW_ENFORCE_PR_ISSUE_STATE: _wrap(build_enforce_request),
        WORKFLOW_TRIAGE_NEW_ISSUES: _wrap(build_triage_request),
        WORKFLOW_CREATE_SPEC_FROM_ISSUE: _wrap(build_create_spec_request),
        WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE: _wrap(
            build_create_implementation_request
        ),
    }


__all__ = [
    "build_builder_registry",
    "build_create_implementation_request",
    "build_create_spec_request",
    "build_enforce_request",
    "build_respond_request",
    "build_review_request",
    "build_triage_request",
    "build_verify_request",
]
