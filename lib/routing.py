"""Map an incoming GitHub webhook event to a target workflow handler.

The webhook receiver in :mod:`api.webhook` invokes :func:`route_event`
with the GitHub event name and the parsed JSON payload. The router
returns a :class:`RouteDecision` describing which Oz workflow (if any)
should run and why. A return value of ``None`` for ``workflow`` means
the event is deliberately ignored — for example, automation-authored
comments, unsupported event types, or PRs that close without changes.

The webhook is the sole delivery surface for the bot behavior that
the control plane drives. The older Actions adapters that used to
mirror these triggers are deleted so webhook dispatch is the only bot
runtime. The remaining ``.github/workflows/`` entry is repository CI
(``run-tests.yml``).

Webhook coverage today:

- ``pull_request`` events route as follows:

  - ``opened`` / ``reopened`` / ``synchronize`` (non-draft) and
    ``ready_for_review`` route to
    ``review-pull-request``.
  - ``review_requested`` routes to ``review-pull-request`` when
    the requested reviewer is ``oz-agent``.
  - ``labeled`` routes to ``review-pull-request`` for the
    ``oz-review`` label and to ``plan-approved`` for the
    ``plan-approved`` label. The ``plan-approved`` workflow runs
    its synchronous side effects (spec-approved comment,
    ``ready-to-spec`` label removal) inline and falls through to
    a ``create-implementation-from-issue`` cloud agent dispatch
    when the linked issue carries ``ready-to-implement`` and
    ``oz-agent`` is assigned.
- ``pull_request_review_comment`` events route to
  ``review-pull-request`` (``/oz-review``), ``verify-pr-comment``
  (``/oz-verify``), or ``respond-to-pr-comment`` (``@oz-agent``).
- ``pull_request_review`` events route to ``respond-to-pr-comment``
  when the review body mentions ``@oz-agent``.
- ``issue_comment`` events on a pull request route to the same set as
  ``pull_request_review_comment`` (GitHub delivers PR conversation
  comments under the ``issue_comment`` event).
- ``issues`` events:

  - ``opened`` routes to ``triage-new-issues`` regardless of the
    issue's existing labels (``ready-to-spec`` /
    ``ready-to-implement`` issues still get a triage pass).
  - ``assigned`` routes to ``create-spec-from-issue`` or
    ``create-implementation-from-issue`` when the assignee being
    added is ``oz-agent`` and the issue carries the matching
    lifecycle label (``ready-to-spec`` /
    ``ready-to-implement``).
  - ``labeled`` routes to ``create-spec-from-issue`` /
    ``create-implementation-from-issue`` when the label being added is
    ``ready-to-spec`` / ``ready-to-implement`` and ``oz-agent`` is
    already among the assignees. When ``oz-agent`` is NOT assigned,
    the same labels route to ``announce-ready-issue`` so the
    webhook can post a one-shot announcement comment letting
    contributors know the issue is open for the matching kind of
    contribution and that maintainers can tag ``@oz-agent`` to start
    automated work.

- ``issue_comment`` events on a plain (non-PR) issue route to
  ``triage-new-issues`` when the comment carries an ``@oz-agent``
  mention and the issue is not already ready for spec or implementation.
  Mentions on ``ready-to-spec`` or ``ready-to-implement`` issues route
  directly to the matching spec or implementation workflow. Replies from
  the original reporter on ``needs-info`` issues also route to triage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Workflow identifiers the dispatcher knows how to handle. These strings
# are used as state-store keys and as ``RouteDecision.workflow`` values
# so adding a new workflow only requires touching the dispatcher and
# this module.
WORKFLOW_REVIEW_PR = "review-pull-request"
WORKFLOW_RESPOND_TO_PR_COMMENT = "respond-to-pr-comment"
WORKFLOW_VERIFY_PR_COMMENT = "verify-pr-comment"
WORKFLOW_TRIAGE_NEW_ISSUES = "triage-new-issues"
WORKFLOW_CREATE_SPEC_FROM_ISSUE = "create-spec-from-issue"
WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE = "create-implementation-from-issue"
WORKFLOW_PLAN_APPROVED = "plan-approved"
WORKFLOW_ANNOUNCE_READY_ISSUE = "announce-ready-issue"

OZ_AGENT_LOGIN = "oz-agent"
OZ_REVIEW_LABEL = "oz-review"
PLAN_APPROVED_LABEL = "plan-approved"
TRIAGED_LABEL = "triaged"
NEEDS_INFO_LABEL = "needs-info"
READY_TO_SPEC_LABEL = "ready-to-spec"
READY_TO_IMPLEMENT_LABEL = "ready-to-implement"

OZ_AGENT_MENTION = "@oz-agent"
OZ_REVIEW_COMMAND = "/oz-review"
OZ_VERIFY_COMMAND = "/oz-verify"


@dataclass(frozen=True)
class RouteDecision:
    """Result of routing an incoming webhook payload.

    ``workflow`` is ``None`` when the event should be skipped without
    dispatching an agent run. ``reason`` is always set so the webhook
    handler can include it in structured logs whether the request was
    routed or dropped.
    """

    workflow: str | None
    reason: str
    extra: dict[str, Any] | None = None


def _label_names(labels: Any) -> list[str]:
    if not isinstance(labels, list):
        return []
    out: list[str] = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = getattr(label, "name", None)
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def _login(actor: Any) -> str:
    if isinstance(actor, dict):
        login = actor.get("login")
    else:
        login = getattr(actor, "login", None)
    return str(login or "").strip()


def _is_bot(actor: Any) -> bool:
    """Return True when *actor* is an automation account.

    Mirrors ``oz.helpers.is_automation_user`` so the control
    plane silently drops bot-authored events without spending API quota
    on them.
    """
    if not isinstance(actor, (dict, object)):
        return False
    user_type = ""
    if isinstance(actor, dict):
        user_type = str(actor.get("type") or "").strip().lower()
    else:
        user_type = str(getattr(actor, "type", "") or "").strip().lower()
    if user_type == "bot":
        return True
    login = _login(actor).lower()
    return bool(login) and login.endswith("[bot]")


def _route_issue_comment(payload: dict[str, Any]) -> RouteDecision:
    action = str(payload.get("action") or "").strip()
    if action not in {"created", "edited"}:
        return RouteDecision(None, f"issue_comment action {action!r} not handled")
    comment = payload.get("comment") or {}
    if not isinstance(comment, dict):
        return RouteDecision(None, "missing comment payload")
    if _is_bot(comment.get("user")):
        return RouteDecision(None, "comment authored by automation user")
    body = str(comment.get("body") or "")
    issue = payload.get("issue") or {}
    if not isinstance(issue, dict):
        return RouteDecision(None, "missing issue payload")
    if not issue.get("pull_request"):
        return _route_plain_issue_comment(issue=issue, comment=comment, body=body)
    if OZ_VERIFY_COMMAND in body:
        return RouteDecision(WORKFLOW_VERIFY_PR_COMMENT, "/oz-verify on PR comment")
    if OZ_REVIEW_COMMAND in body:
        return RouteDecision(WORKFLOW_REVIEW_PR, "/oz-review on PR comment")
    if OZ_AGENT_MENTION in body:
        return RouteDecision(WORKFLOW_RESPOND_TO_PR_COMMENT, "@oz-agent mention on PR")
    return RouteDecision(None, "PR comment without Oz command or mention")


def _route_plain_issue_comment(
    *,
    issue: dict[str, Any],
    comment: dict[str, Any],
    body: str,
) -> RouteDecision:
    """Route an ``issue_comment`` event on a plain (non-PR) issue.

    Triage runs are dispatched for ``@oz-agent`` mentions unless the
    issue has already moved into a ready-for-work lifecycle state. On
    ``ready-to-spec`` issues, a mention starts or refreshes the spec
    workflow; on ``ready-to-implement`` issues, it starts or refreshes
    the implementation workflow. Other mentions route to triage, whose
    ``comment_type`` discriminator decides whether to emit a full triage
    mutation or a lighter response-style comment.

    Replies from the original issue author on a ``needs-info`` issue
    (without an explicit mention) also trigger a re-triage so the bot
    picks up the new context the reporter just supplied.
    """
    labels = _label_names(issue.get("labels"))
    has_mention = OZ_AGENT_MENTION in body
    if has_mention:
        # ``ready-to-implement`` and ``ready-to-spec`` issues are
        # already past triage — a maintainer pinging ``@oz-agent``
        # there is asking the bot to start (or refresh) the
        # implementation / spec PR rather than to re-triage. Check
        # the implementation label first so issues that somehow
        # carry both labels at once (e.g. mid-promotion) skip the
        # spec stage.
        if READY_TO_IMPLEMENT_LABEL in labels:
            return RouteDecision(
                WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
                "@oz-agent mention on ready-to-implement issue",
            )
        if READY_TO_SPEC_LABEL in labels:
            return RouteDecision(
                WORKFLOW_CREATE_SPEC_FROM_ISSUE,
                "@oz-agent mention on ready-to-spec issue",
            )
        return RouteDecision(
            WORKFLOW_TRIAGE_NEW_ISSUES,
            "@oz-agent mention triggers (re-)triage",
        )
    if NEEDS_INFO_LABEL in labels:
        commenter_login = _login(comment.get("user"))
        author_login = _login(issue.get("user"))
        if commenter_login and author_login and commenter_login == author_login:
            return RouteDecision(
                WORKFLOW_TRIAGE_NEW_ISSUES,
                "needs-info reply from issue author",
            )
    return RouteDecision(
        None,
        "plain issue comment without Oz mention or needs-info reply",
    )


def _route_issues(payload: dict[str, Any]) -> RouteDecision:
    """Route an ``issues`` webhook event.

    Three actions are routed:

    - ``opened`` triggers a fresh triage pass regardless of the
      issue's existing labels. Issues that arrive with prior
      lifecycle labels (``ready-to-spec``, ``ready-to-implement``,
      etc.) — for example because they were imported from another
      repo or re-opened — still get a triage pass so the bot can
      post a fresh progress comment and pick up any state changes
      that landed while the issue was closed.
    - ``assigned`` triggers ``create-spec-from-issue`` or
      ``create-implementation-from-issue`` when the assignee being
      added is ``oz-agent`` itself and the issue carries the
      matching lifecycle label. Operators assigning humans use this
      event for their own tracking and the bot stays out of it.
    - ``labeled`` triggers the same workflows when the label being
      added is ``ready-to-spec`` or ``ready-to-implement`` and
      ``oz-agent`` is already among the issue assignees.

    Both ``assigned`` and ``labeled`` are inherently trust-safe:
    GitHub only allows repository collaborators (triage permission
    or higher) to assign or label issues, so there is no separate
    membership probe here. ``ready-to-implement`` wins over
    ``ready-to-spec`` when an issue carries both labels so the bot
    does not regenerate a spec for an issue that has already moved
    to implementation.
    """
    action = str(payload.get("action") or "").strip()
    issue = payload.get("issue") or {}
    if not isinstance(issue, dict):
        return RouteDecision(None, "missing issue payload")
    if issue.get("pull_request"):
        # GitHub mirrors PRs into the issues feed; the dedicated
        # ``pull_request`` route already covers them.
        return RouteDecision(
            None, f"issues.{action} delivered for a pull request"
        )
    if action == "opened":
        if _is_bot(issue.get("user")):
            return RouteDecision(None, "issue authored by automation user")
        return RouteDecision(
            WORKFLOW_TRIAGE_NEW_ISSUES, "issues.opened triggers triage"
        )
    if action == "assigned":
        # Only fire when the assignee being added is ``oz-agent``
        # itself — maintainers assigning humans use this event for
        # their own tracking and the bot must stay out of it.
        assignee_login = _login(payload.get("assignee"))
        if assignee_login != OZ_AGENT_LOGIN:
            return RouteDecision(
                None,
                f"issues.assigned for non-oz-agent assignee {assignee_login!r}",
            )
        labels = _label_names(issue.get("labels"))
        if READY_TO_IMPLEMENT_LABEL in labels:
            return RouteDecision(
                WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
                "oz-agent assigned to ready-to-implement issue",
            )
        if READY_TO_SPEC_LABEL in labels:
            return RouteDecision(
                WORKFLOW_CREATE_SPEC_FROM_ISSUE,
                "oz-agent assigned to ready-to-spec issue",
            )
        return RouteDecision(
            None,
            "oz-agent assigned to issue without ready-to-spec or ready-to-implement label",
        )
    if action == "labeled":
        # Lifecycle labels (``ready-to-spec`` / ``ready-to-implement``)
        # split into two routes depending on whether ``oz-agent`` is
        # already on the issue:
        #
        # - oz-agent assigned -> the bot has been enlisted to do the
        #   work itself, so route to ``create-spec-from-issue`` /
        #   ``create-implementation-from-issue`` and let the cloud
        #   agent handle it.
        # - oz-agent NOT assigned -> the maintainer has merely opened
        #   the issue up for community contributions, so route to
        #   ``announce-ready-issue`` to post a one-shot announcement
        #   comment instead.
        label_name = str((payload.get("label") or {}).get("name") or "").strip()
        if label_name not in {READY_TO_SPEC_LABEL, READY_TO_IMPLEMENT_LABEL}:
            return RouteDecision(
                None, f"unhandled label {label_name!r} on issue"
            )
        assignees = [
            _login(assignee)
            for assignee in issue.get("assignees") or []
            if isinstance(assignee, dict)
        ]
        if OZ_AGENT_LOGIN not in assignees:
            return RouteDecision(
                WORKFLOW_ANNOUNCE_READY_ISSUE,
                f"{label_name!r} added without oz-agent assignee; "
                "announcing availability for community contribution",
                extra={"label": label_name},
            )
        if label_name == READY_TO_IMPLEMENT_LABEL:
            return RouteDecision(
                WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
                "ready-to-implement label added with oz-agent assignee",
            )
        return RouteDecision(
            WORKFLOW_CREATE_SPEC_FROM_ISSUE,
            "ready-to-spec label added with oz-agent assignee",
        )
    return RouteDecision(None, f"issues action {action!r} not handled")


def _route_pull_request(payload: dict[str, Any]) -> RouteDecision:
    action = str(payload.get("action") or "").strip()
    pr = payload.get("pull_request") or {}
    if not isinstance(pr, dict):
        return RouteDecision(None, "missing pull_request payload")
    if pr.get("state") != "open":
        return RouteDecision(None, "pull_request is not open")
    if action in {"opened", "reopened", "synchronize"} and not pr.get("draft", False):
        return RouteDecision(
            WORKFLOW_REVIEW_PR,
            f"pull_request {action} (non-draft)",
        )
    if action == "ready_for_review":
        return RouteDecision(WORKFLOW_REVIEW_PR, "pull_request ready_for_review")
    if action == "review_requested":
        requested = ((payload.get("requested_reviewer") or {}).get("login") or "").strip()
        if requested == OZ_AGENT_LOGIN:
            return RouteDecision(WORKFLOW_REVIEW_PR, "review requested from oz-agent")
        return RouteDecision(None, "review requested from non-Oz reviewer")
    if action == "labeled":
        label_name = ((payload.get("label") or {}).get("name") or "").strip()
        if label_name == OZ_REVIEW_LABEL:
            return RouteDecision(WORKFLOW_REVIEW_PR, "oz-review label applied")
        if label_name == PLAN_APPROVED_LABEL:
            # Only repository collaborators can label a PR, so the
            # ``plan-approved`` route is inherently trust-safe. The
            # webhook handler runs the synchronous comment +
            # label-removal side effects inline and falls through
            # to a ``create-implementation-from-issue`` cloud agent
            # dispatch when the linked issue is ready for it.
            return RouteDecision(
                WORKFLOW_PLAN_APPROVED, "plan-approved label applied"
            )
        return RouteDecision(None, f"unhandled label {label_name!r} on PR")
    return RouteDecision(None, f"pull_request action {action!r} not handled")


def _route_pull_request_review_comment(payload: dict[str, Any]) -> RouteDecision:
    action = str(payload.get("action") or "").strip()
    if action != "created":
        return RouteDecision(None, f"pull_request_review_comment action {action!r} not handled")
    comment = payload.get("comment") or {}
    if not isinstance(comment, dict):
        return RouteDecision(None, "missing review comment payload")
    if _is_bot(comment.get("user")):
        return RouteDecision(None, "review comment authored by automation user")
    body = str(comment.get("body") or "")
    if OZ_REVIEW_COMMAND in body:
        return RouteDecision(WORKFLOW_REVIEW_PR, "/oz-review on review comment")
    if OZ_VERIFY_COMMAND in body:
        return RouteDecision(WORKFLOW_VERIFY_PR_COMMENT, "/oz-verify on review comment")
    if OZ_AGENT_MENTION in body:
        return RouteDecision(
            WORKFLOW_RESPOND_TO_PR_COMMENT,
            "@oz-agent mention on review comment",
        )
    return RouteDecision(None, "review comment without Oz command or mention")


def _route_pull_request_review(payload: dict[str, Any]) -> RouteDecision:
    action = str(payload.get("action") or "").strip()
    if action not in {"submitted", "edited"}:
        return RouteDecision(None, f"pull_request_review action {action!r} not handled")
    review = payload.get("review") or {}
    if not isinstance(review, dict):
        return RouteDecision(None, "missing review payload")
    if _is_bot(review.get("user")):
        return RouteDecision(None, "review authored by automation user")
    body = str(review.get("body") or "")
    if OZ_AGENT_MENTION in body:
        return RouteDecision(WORKFLOW_RESPOND_TO_PR_COMMENT, "@oz-agent mention in PR review body")
    return RouteDecision(None, "review body without Oz mention")


_EVENT_HANDLERS = {
    "issue_comment": _route_issue_comment,
    "issues": _route_issues,
    "pull_request": _route_pull_request,
    "pull_request_review": _route_pull_request_review,
    "pull_request_review_comment": _route_pull_request_review_comment,
}


def route_event(event: str, payload: dict[str, Any]) -> RouteDecision:
    """Decide which workflow (if any) handles *event* + *payload*.

    The router never raises on unknown events or malformed payloads; it
    returns a ``RouteDecision`` with ``workflow=None`` and a structured
    reason so the webhook handler can log+drop without aborting.
    """
    if not isinstance(payload, dict):
        return RouteDecision(None, "non-object webhook payload")
    handler = _EVENT_HANDLERS.get(event)
    if handler is None:
        return RouteDecision(None, f"event {event!r} not handled")
    return handler(payload)


__all__ = [
    "NEEDS_INFO_LABEL",
    "OZ_AGENT_LOGIN",
    "OZ_AGENT_MENTION",
    "OZ_REVIEW_COMMAND",
    "OZ_VERIFY_COMMAND",
    "OZ_REVIEW_LABEL",
    "PLAN_APPROVED_LABEL",
    "READY_TO_IMPLEMENT_LABEL",
    "READY_TO_SPEC_LABEL",
    "RouteDecision",
    "TRIAGED_LABEL",
    "WORKFLOW_ANNOUNCE_READY_ISSUE",
    "WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE",
    "WORKFLOW_CREATE_SPEC_FROM_ISSUE",
    "WORKFLOW_PLAN_APPROVED",
    "WORKFLOW_RESPOND_TO_PR_COMMENT",
    "WORKFLOW_REVIEW_PR",
    "WORKFLOW_TRIAGE_NEW_ISSUES",
    "WORKFLOW_VERIFY_PR_COMMENT",
    "route_event",
]
