"""Map an incoming GitHub webhook event to a target workflow handler.

The control-plane webhook handler invokes :func:`route_event` with the
GitHub event name and the parsed JSON payload. The router returns a
:class:`RouteDecision` describing which Oz workflow (if any) should run
and why. A return value of ``None`` for ``workflow`` means the event is
deliberately ignored — for example, automation-authored comments,
unsupported event types, or PRs that close without changes.

The routing table mirrors the conditions baked into
``.github/workflows/*.yml`` today so flipping the GitHub App webhook
from GitHub Actions to the Vercel control plane preserves behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Workflow identifiers the dispatcher knows how to handle. These strings
# are used as state-store keys and as ``RouteDecision.workflow`` values
# so adding a new workflow only requires touching the dispatcher and
# this module.
WORKFLOW_TRIAGE = "triage-new-issues"
WORKFLOW_RESPOND_TRIAGED = "respond-to-triaged-issue-comment"
WORKFLOW_REVIEW_PR = "review-pull-request"
WORKFLOW_CREATE_SPEC = "create-spec-from-issue"
WORKFLOW_CREATE_IMPLEMENTATION = "create-implementation-from-issue"
WORKFLOW_RESPOND_TO_PR_COMMENT = "respond-to-pr-comment"
WORKFLOW_VERIFY_PR_COMMENT = "verify-pr-comment"
WORKFLOW_ENFORCE_PR_ISSUE_STATE = "enforce-pr-issue-state"

OZ_AGENT_LOGIN = "oz-agent"
READY_TO_SPEC_LABEL = "ready-to-spec"
READY_TO_IMPLEMENT_LABEL = "ready-to-implement"
TRIAGED_LABEL = "triaged"
OZ_REVIEW_LABEL = "oz-review"

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


def _route_issues(payload: dict[str, Any]) -> RouteDecision:
    action = str(payload.get("action") or "").strip()
    issue = payload.get("issue") or {}
    if not isinstance(issue, dict):
        return RouteDecision(None, "missing issue payload")
    if issue.get("pull_request"):
        return RouteDecision(None, "issue payload describes a pull request")
    labels = _label_names(issue.get("labels"))
    assignees = [_login(a) for a in issue.get("assignees") or []]
    if action in {"opened", "reopened", "edited"}:
        return RouteDecision(WORKFLOW_TRIAGE, f"issue {action}")
    if action == "labeled":
        label_name = ((payload.get("label") or {}).get("name") or "").strip()
        if label_name == READY_TO_SPEC_LABEL and OZ_AGENT_LOGIN in assignees:
            return RouteDecision(WORKFLOW_CREATE_SPEC, "ready-to-spec on assigned issue")
        if label_name == READY_TO_IMPLEMENT_LABEL and OZ_AGENT_LOGIN in assignees:
            return RouteDecision(
                WORKFLOW_CREATE_IMPLEMENTATION,
                "ready-to-implement on assigned issue",
            )
        return RouteDecision(WORKFLOW_TRIAGE, f"label {label_name!r} added")
    if action == "assigned":
        assignee_login = ((payload.get("assignee") or {}).get("login") or "").strip()
        if assignee_login != OZ_AGENT_LOGIN:
            return RouteDecision(None, "issue assigned to a non-Oz user")
        if READY_TO_SPEC_LABEL in labels:
            return RouteDecision(WORKFLOW_CREATE_SPEC, "oz-agent assigned with ready-to-spec")
        if READY_TO_IMPLEMENT_LABEL in labels:
            return RouteDecision(
                WORKFLOW_CREATE_IMPLEMENTATION,
                "oz-agent assigned with ready-to-implement",
            )
        return RouteDecision(None, "oz-agent assigned without ready label")
    return RouteDecision(None, f"issues action {action!r} not handled")


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
    is_pr = bool(issue.get("pull_request"))
    labels = _label_names(issue.get("labels"))
    if is_pr:
        if OZ_VERIFY_COMMAND in body:
            return RouteDecision(WORKFLOW_VERIFY_PR_COMMENT, "/oz-verify on PR comment")
        if OZ_REVIEW_COMMAND in body:
            return RouteDecision(WORKFLOW_REVIEW_PR, "/oz-review on PR comment")
        if OZ_AGENT_MENTION in body:
            return RouteDecision(WORKFLOW_RESPOND_TO_PR_COMMENT, "@oz-agent mention on PR")
        return RouteDecision(None, "PR comment without Oz command or mention")
    # Plain issue comments.
    if OZ_AGENT_MENTION in body and READY_TO_SPEC_LABEL in labels:
        return RouteDecision(WORKFLOW_CREATE_SPEC, "@oz-agent mention on ready-to-spec issue")
    if (
        OZ_AGENT_MENTION in body
        and TRIAGED_LABEL in labels
        and READY_TO_SPEC_LABEL not in labels
        and READY_TO_IMPLEMENT_LABEL not in labels
    ):
        return RouteDecision(
            WORKFLOW_RESPOND_TRIAGED,
            "@oz-agent mention on triaged issue without ready labels",
        )
    return RouteDecision(WORKFLOW_TRIAGE, "issue comment retriage")


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
    "issues": _route_issues,
    "issue_comment": _route_issue_comment,
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
    "OZ_AGENT_LOGIN",
    "OZ_AGENT_MENTION",
    "OZ_REVIEW_COMMAND",
    "OZ_VERIFY_COMMAND",
    "OZ_REVIEW_LABEL",
    "READY_TO_IMPLEMENT_LABEL",
    "READY_TO_SPEC_LABEL",
    "RouteDecision",
    "TRIAGED_LABEL",
    "WORKFLOW_CREATE_IMPLEMENTATION",
    "WORKFLOW_CREATE_SPEC",
    "WORKFLOW_ENFORCE_PR_ISSUE_STATE",
    "WORKFLOW_RESPOND_TO_PR_COMMENT",
    "WORKFLOW_RESPOND_TRIAGED",
    "WORKFLOW_REVIEW_PR",
    "WORKFLOW_TRIAGE",
    "WORKFLOW_VERIFY_PR_COMMENT",
    "route_event",
]
