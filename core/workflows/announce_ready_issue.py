"""Synchronous handler for the ``announce-ready-issue`` webhook flow.

The webhook routes ``issues.labeled`` deliveries to this handler when the
applied label is ``ready-to-spec`` / ``ready-to-implement`` AND ``oz-agent``
is NOT among the issue's assignees. In that case the maintainer has merely
opened the issue up for community contribution (rather than enlisting the
bot to do the work), so the webhook posts a one-shot announcement comment
on the issue letting contributors know:

- that the issue is open for the matching kind of contribution
  (a code-change PR for ``ready-to-implement``; a product/tech spec PR for
  ``ready-to-spec``), and
- that anyone can tag ``@oz-agent`` in a comment on the issue to have
  the bot pick up the work automatically.

The handler also assigns the issue to whoever applied the label
(APP-5520), so a maintainer opening an issue for community contribution
doesn't leave it unassigned. This only happens when the issue currently
has no assignee at all and the labeler is not an automation account; a
failed assignment call is logged but never suppresses the announcement
comment below.

The handler is fully synchronous — there is no cloud agent to dispatch —
and runs inline inside the Vercel webhook function. Idempotency is
enforced via a workflow-scoped ``oz-agent-metadata`` marker so retried
webhook deliveries do not double-post the announcement.

This module owns the webhook-era replacement for the deleted
ready-issue announcement adapters, so the Vercel function is the single
runtime for this behavior.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from github.Repository import Repository

from oz.helpers import (
    _workflow_metadata_prefix,
    comment_metadata,
    get_login,
    is_automation_user,
)

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "announce-ready-issue"
READY_TO_SPEC_LABEL = "ready-to-spec"
READY_TO_IMPLEMENT_LABEL = "ready-to-implement"
OZ_AGENT_LOGIN = "oz-agent"

_SUPPORTED_LABELS = {READY_TO_SPEC_LABEL, READY_TO_IMPLEMENT_LABEL}


def _build_announcement_body(label_name: str) -> str:
    """Return the announcement comment body for *label_name*.

    The wording differs between the two labels because the kind of
    contribution that's invited is different (code change vs. spec
    proposal). Both bodies invite contributors to submit a PR directly
    AND tell users they can tag ``@oz-agent`` to have the bot
    pick up the work automatically.
    """
    if label_name == READY_TO_IMPLEMENT_LABEL:
        return (
            "This issue has been labeled `ready-to-implement` and is open "
            "for contributions involving code changes. If you'd like to "
            "tackle it, feel free to open a pull request against this "
            "issue. You can also comment `@oz-agent` on this "
            "issue to have the bot draft an implementation PR "
            "automatically."
        )
    # READY_TO_SPEC_LABEL
    return (
        "This issue has been labeled `ready-to-spec` and is open for "
        "contributions in the form of a product or technical spec. "
        "If you'd like to draft one, feel free to open a pull request "
        "with the spec under `specs/`. You can also comment "
        "`@oz-agent` on this issue to have the bot draft the spec "
        "automatically."
    )


def _maybe_assign_labeler(
    issue_handle: Any,
    *,
    assignee_logins: set[str],
    sender: Any,
) -> dict[str, Any]:
    """Assign the labeler to *issue_handle* when eligible (APP-5520).

    Only assigns when the issue currently has no assignee at all — this
    never adds the labeler alongside an existing assignee — and never
    assigns when the labeling actor is an automation account (e.g. a
    bot re-applying the label). The assignment call is best effort: a
    failure is logged and returned as ``assigned: False`` so the caller
    still posts the announcement comment.
    """
    if assignee_logins:
        return {"assigned": False, "reason": "issue already has an assignee"}
    if is_automation_user(sender):
        return {"assigned": False, "reason": "label applied by automation user"}
    sender_login = get_login(sender)
    if not sender_login:
        return {"assigned": False, "reason": "missing sender login"}
    try:
        issue_handle.add_to_assignees(sender_login)
    except Exception:
        logger.exception(
            "Failed to assign %s to issue #%s after ready-to-* label",
            sender_login,
            getattr(issue_handle, "number", "?"),
        )
        return {
            "assigned": False,
            "reason": "assignment API call failed",
            "login": sender_login,
        }
    return {"assigned": True, "login": sender_login}


def _existing_announcement_comment(
    issue_handle: Any, *, prefix: str
) -> Any | None:
    """Return a prior announcement comment on *issue_handle* if any.

    Idempotency is enforced via the same workflow-prefix metadata
    marker pattern as :mod:`workflows.plan_approved`. GitHub retries
    failed webhook deliveries, so the helper has to detect "we
    already announced this issue" without scanning every comment for
    the natural-language wording.
    """
    try:
        comments = list(issue_handle.get_comments())
    except Exception:
        logger.exception(
            "Failed to list comments while deduping announce-ready-issue post"
        )
        return None
    for comment in comments:
        body = str(getattr(comment, "body", "") or "")
        if prefix in body:
            return comment
    return None


def apply_announce_ready_issue_sync(
    repo_handle: Repository,
    *,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Run the synchronous side effect for an ``announce-ready-issue`` event.

    Returns a structured outcome the webhook surfaces in the 202
    response body. The handler always returns a non-``None`` outcome
    because the webhook never falls through to a cloud-agent dispatch
    for this workflow.

    Outcomes:

    - ``{"action": "skipped", "reason": ...}`` when the payload is
      malformed (missing issue / repository / label) or the labeled
      issue carries an ``oz-agent`` assignee (the routing layer
      already prefers the spec/implementation flow in that case but
      we re-validate here to keep the sync helper safe in isolation).
    - ``{"action": "announced", ...}`` when a fresh announcement was
      posted.
    - ``{"action": "noop", ...}`` when a prior announcement already
      exists for the same workflow + issue.
    """
    issue_payload = payload.get("issue") or {}
    if not isinstance(issue_payload, dict):
        return {"action": "skipped", "reason": "missing issue payload"}
    issue_number = int(issue_payload.get("number") or 0)
    if issue_number <= 0:
        return {"action": "skipped", "reason": "missing issue_number"}
    if str(issue_payload.get("state") or "open") != "open":
        return {
            "action": "skipped",
            "reason": "issue is not open",
            "issue_number": issue_number,
        }

    repo_payload = payload.get("repository") or {}
    full_name = str(repo_payload.get("full_name") or "")
    if "/" not in full_name:
        return {"action": "skipped", "reason": "missing repository.full_name"}
    owner, repo = full_name.split("/", 1)

    label_payload = payload.get("label") or {}
    label_name = str(label_payload.get("name") or "").strip()
    if label_name not in _SUPPORTED_LABELS:
        return {
            "action": "skipped",
            "reason": f"unsupported label {label_name!r}",
            "issue_number": issue_number,
        }

    # Re-validate the assignee gate so the sync helper stays safe even
    # when invoked outside the routing layer (for example, by a future
    # caller that wants to broadcast announcements unconditionally).
    assignee_logins = {
        str((assignee or {}).get("login") or "")
        for assignee in (issue_payload.get("assignees") or [])
        if isinstance(assignee, dict)
    }
    if OZ_AGENT_LOGIN in assignee_logins:
        return {
            "action": "skipped",
            "reason": "oz-agent is already assigned; spec/implementation flow handles it",
            "issue_number": issue_number,
            "label": label_name,
        }

    issue_handle = repo_handle.get_issue(int(issue_number))

    # Assign the issue to whoever applied the label, per APP-5520. This
    # runs before the comment idempotency check below: it is naturally
    # idempotent itself (a retried delivery either still sees no
    # assignee, in which case re-assigning the same login is a no-op
    # on GitHub's side, or sees the assignment already landed, in
    # which case ``assignee_logins`` above would have short-circuited).
    assignment = _maybe_assign_labeler(
        issue_handle,
        assignee_logins=assignee_logins,
        sender=payload.get("sender"),
    )

    # Idempotency: skip the comment post when a prior announcement
    # already exists. The metadata prefix pins the dedupe to this
    # workflow + issue so retried deliveries (or repeated label
    # toggling) do not double-post.
    metadata_prefix = _workflow_metadata_prefix(WORKFLOW_NAME, int(issue_number))
    if _existing_announcement_comment(issue_handle, prefix=metadata_prefix) is not None:
        return {
            "action": "noop",
            "reason": "announcement already posted for this issue",
            "issue_number": issue_number,
            "label": label_name,
            "assignee_added": assignment["assigned"],
        }

    body = _build_announcement_body(label_name)
    metadata = comment_metadata(WORKFLOW_NAME, int(issue_number))
    try:
        issue_handle.create_comment(f"{body}\n\n{metadata}")
    except Exception:
        logger.exception(
            "Failed to post announce-ready-issue comment on issue #%s in %s/%s",
            issue_number,
            owner,
            repo,
        )
        return {
            "action": "skipped",
            "reason": "failed to post announcement comment",
            "issue_number": issue_number,
            "label": label_name,
            "assignee_added": assignment["assigned"],
        }

    return {
        "action": "announced",
        "issue_number": issue_number,
        "label": label_name,
        "assignee_added": assignment["assigned"],
    }


__all__ = [
    "OZ_AGENT_LOGIN",
    "READY_TO_IMPLEMENT_LABEL",
    "READY_TO_SPEC_LABEL",
    "WORKFLOW_NAME",
    "apply_announce_ready_issue_sync",
]
