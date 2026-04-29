"""Map an incoming GitHub webhook event to a target workflow handler.

The webhook receiver in :mod:`api.webhook` invokes :func:`route_event`
with the GitHub event name and the parsed JSON payload. The router
returns a :class:`RouteDecision` describing which Oz workflow (if any)
should run and why. A return value of ``None`` for ``workflow`` means
the event is deliberately ignored — for example, automation-authored
comments, unsupported event types, or PRs that close without changes.

The webhook is the sole delivery surface for the bot behavior that the
control plane drives. Plan-approval workflows and a handful of related
issue helpers (``comment-on-unready-assigned-issue``, ``create-spec``,
``create-implementation``, …) still flow through the legacy GitHub
Actions wrappers under ``.github/workflows/`` because they perform
repository-mutating work (cloning repos, pushing branches, opening
PRs) that is easier to express as a job in the GitHub Actions runtime
than as a fire-and-forget cloud agent dispatch. The router
intentionally drops those events so the webhook does not double-fire
alongside the GitHub Actions workflow.

Webhook coverage today:

- ``pull_request`` events (``opened``, ``ready_for_review``,
  ``review_requested``, ``synchronize``/``edited``, ``labeled``) route
  to ``review-pull-request`` or ``enforce-pr-issue-state``.
- ``pull_request_review_comment`` events route to
  ``review-pull-request`` (``/oz-review``), ``verify-pr-comment``
  (``/oz-verify``), or ``respond-to-pr-comment`` (``@oz-agent``).
- ``issue_comment`` events on a pull request route to the same set as
  ``pull_request_review_comment`` (GitHub delivers PR conversation
  comments under the ``issue_comment`` event).
- ``issues`` events on a freshly opened, non-``triaged`` issue route
  to ``triage-new-issues``.
- ``issue_comment`` events on a plain (non-PR) issue route to
  ``triage-new-issues`` when the comment carries an ``@oz-agent``
  mention against a non-``triaged`` issue, or when the issue carries
  the ``needs-info`` label and the comment is the original reporter
  replying without a mention.

The ``respond-to-triaged-issue-comment`` workflow (an ``@oz-agent``
mention on an already-``triaged`` issue) still flows through GitHub
Actions and is intentionally NOT routed here so the two delivery
paths do not double-fire on the same comment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Workflow identifiers the dispatcher knows how to handle. These strings
# are used as state-store keys and as ``RouteDecision.workflow`` values
# so adding a new workflow only requires touching the dispatcher and
# this module. Issue-triggered and plan-approval workflows live in
# ``.github/workflows/`` and are intentionally not exposed here so the
# webhook does not race with the GitHub Actions runtime.
WORKFLOW_REVIEW_PR = "review-pull-request"
WORKFLOW_RESPOND_TO_PR_COMMENT = "respond-to-pr-comment"
WORKFLOW_VERIFY_PR_COMMENT = "verify-pr-comment"
WORKFLOW_ENFORCE_PR_ISSUE_STATE = "enforce-pr-issue-state"
WORKFLOW_TRIAGE_NEW_ISSUES = "triage-new-issues"

OZ_AGENT_LOGIN = "oz-agent"
OZ_REVIEW_LABEL = "oz-review"
TRIAGED_LABEL = "triaged"
NEEDS_INFO_LABEL = "needs-info"

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

    Mirrors ``oz_workflows.helpers.is_automation_user`` so the control
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

    Mirrors the gating that the legacy GitHub Actions workflow
    (``triage-new-issues-local.yml``) applies before invoking the
    triage entrypoint:

    - An ``@oz-agent`` mention on a non-``triaged`` issue triggers a
      first-pass triage so the reporter can ping the bot manually.
    - A reply from the original issue author on a ``needs-info`` issue
      (without an explicit mention) triggers a re-triage so the bot
      picks up the new context the reporter just supplied.
    - An ``@oz-agent`` mention on an already-``triaged`` issue stays
      on the GitHub Actions delivery path because
      ``respond-to-triaged-issue-comment`` is not yet wired into the
      webhook control plane.
    """
    labels = _label_names(issue.get("labels"))
    is_triaged = TRIAGED_LABEL in labels
    has_mention = OZ_AGENT_MENTION in body
    if has_mention:
        if is_triaged:
            return RouteDecision(
                None,
                "@oz-agent mention on a triaged issue handled by GitHub Actions",
            )
        return RouteDecision(
            WORKFLOW_TRIAGE_NEW_ISSUES,
            "@oz-agent mention on a non-triaged issue",
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

    Triage runs are dispatched on ``opened`` for issues that have not
    already been triaged. Other actions (``edited``, ``labeled``,
    ``assigned``, …) stay on their GitHub Actions delivery paths
    (e.g. ``comment-on-unready-assigned-issue.yml``) so this router
    does not race with workflows that mutate issue state directly.
    """
    action = str(payload.get("action") or "").strip()
    issue = payload.get("issue") or {}
    if not isinstance(issue, dict):
        return RouteDecision(None, "missing issue payload")
    if action != "opened":
        return RouteDecision(None, f"issues action {action!r} not handled")
    if issue.get("pull_request"):
        # GitHub mirrors PRs into the issues feed; the dedicated
        # ``pull_request`` route already covers them.
        return RouteDecision(None, "issues.opened delivered for a pull request")
    if _is_bot(issue.get("user")):
        return RouteDecision(None, "issue authored by automation user")
    labels = _label_names(issue.get("labels"))
    if TRIAGED_LABEL in labels:
        return RouteDecision(
            None,
            "issues.opened on an already-triaged issue",
        )
    return RouteDecision(
        WORKFLOW_TRIAGE_NEW_ISSUES, "issues.opened on a non-triaged issue"
    )


def _route_pull_request(payload: dict[str, Any]) -> RouteDecision:
    action = str(payload.get("action") or "").strip()
    pr = payload.get("pull_request") or {}
    if not isinstance(pr, dict):
        return RouteDecision(None, "missing pull_request payload")
    if pr.get("state") != "open":
        return RouteDecision(None, "pull_request is not open")
    if action == "opened" and not pr.get("draft", False):
        return RouteDecision(WORKFLOW_REVIEW_PR, "pull_request opened (non-draft)")
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
        return RouteDecision(None, f"unhandled label {label_name!r} on PR")
    if action in {"synchronize", "edited"}:
        return RouteDecision(WORKFLOW_ENFORCE_PR_ISSUE_STATE, f"pull_request {action}")
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


_EVENT_HANDLERS = {
    "issue_comment": _route_issue_comment,
    "issues": _route_issues,
    "pull_request": _route_pull_request,
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
    "RouteDecision",
    "TRIAGED_LABEL",
    "WORKFLOW_ENFORCE_PR_ISSUE_STATE",
    "WORKFLOW_RESPOND_TO_PR_COMMENT",
    "WORKFLOW_REVIEW_PR",
    "WORKFLOW_TRIAGE_NEW_ISSUES",
    "WORKFLOW_VERIFY_PR_COMMENT",
    "route_event",
]
