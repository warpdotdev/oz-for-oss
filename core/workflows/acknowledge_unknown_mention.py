"""Synchronous handler for the ``acknowledge-unknown-mention`` webhook flow.

The webhook routes a PR conversation comment, inline review comment, or
PR review body to this handler when it mentions a handle that *looks*
like the Oz agent but is not recognized — for example the pre-rebrand
typo ``@warp-bot`` or a mistyped ``@ozagent``. The recognized handles
(``@oz-agent`` and the legacy ``@warp-agent`` alias) are handled by the
``respond-to-pr-comment`` flow instead.

Before this handler existed, an unrecognized handle was dropped
silently, so a user who typed the wrong handle got no agent response and
no error and assumed the integration was down. This handler closes that
gap by posting a one-shot acknowledgement comment that tells the user the
mention was received but not recognized, and points them at the correct
``@oz-agent`` handle.

The handler is fully synchronous — there is no cloud agent to dispatch —
and runs inline inside the Vercel webhook function. Idempotency is
enforced via a metadata marker keyed on the triggering comment/review id
so retried webhook deliveries (or multiple unrecognized mentions on the
same PR) do not double-post.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from github.Repository import Repository

from core.routing import find_unrecognized_agent_mention

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "acknowledge-unknown-mention"
OZ_AGENT_MENTION = "@oz-agent"
WARP_AGENT_MENTION = "@warp-agent"


def _resolve_pr_number(payload: Mapping[str, Any]) -> int:
    """Return the PR/issue number the triggering comment lives on."""
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict) and pull_request.get("number") is not None:
        return int(pull_request.get("number") or 0)
    issue = payload.get("issue")
    if isinstance(issue, dict) and issue.get("number") is not None:
        return int(issue.get("number") or 0)
    return 0


def _resolve_trigger(payload: Mapping[str, Any]) -> tuple[int, str]:
    """Return the ``(id, body)`` of the comment or review that triggered the event."""
    comment = payload.get("comment")
    if isinstance(comment, dict):
        return int(comment.get("id") or 0), str(comment.get("body") or "")
    review = payload.get("review")
    if isinstance(review, dict):
        return int(review.get("id") or 0), str(review.get("body") or "")
    return 0, ""


def _acknowledgement_metadata(pr_number: int, trigger_comment_id: int) -> str:
    """Return the idempotency metadata marker for an acknowledgement.

    The marker embeds the triggering comment/review id so each
    unrecognized mention is acknowledged at most once even when the same
    PR receives several of them, while retried webhook deliveries for the
    same comment stay deduplicated.
    """
    payload: dict[str, Any] = {
        "type": "issue-status",
        "workflow": WORKFLOW_NAME,
        "issue": int(pr_number),
        "trigger_comment": int(trigger_comment_id),
    }
    return f"<!-- oz-agent-metadata: {json.dumps(payload, separators=(',', ':'))} -->"


def _build_acknowledgement_body(handle: str) -> str:
    """Return the acknowledgement comment body for an unrecognized *handle*."""
    return (
        f"I noticed you mentioned `@{handle}`, which isn't a handle I "
        "recognize, so I didn't start a run. To have me take a look, "
        f"mention `{OZ_AGENT_MENTION}` (the legacy `{WARP_AGENT_MENTION}` "
        "handle works too)."
    )


def _existing_acknowledgement(issue_handle: Any, *, marker: str) -> Any | None:
    """Return a prior acknowledgement comment matching *marker*, if any."""
    try:
        comments = list(issue_handle.get_comments())
    except Exception:
        logger.exception(
            "Failed to list comments while deduping acknowledge-unknown-mention post"
        )
        return None
    for comment in comments:
        body = str(getattr(comment, "body", "") or "")
        if marker in body:
            return comment
    return None


def apply_acknowledge_unknown_mention_sync(
    repo_handle: Repository,
    *,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Run the synchronous side effect for an ``acknowledge-unknown-mention`` event.

    Returns a structured outcome the webhook surfaces in the 202 response
    body. The handler always returns a non-``None`` outcome because the
    webhook never falls through to a cloud-agent dispatch for this
    workflow.

    Outcomes:

    - ``{"action": "skipped", "reason": ...}`` when the payload is
      malformed (missing PR number / repository) or the triggering body
      no longer carries an unrecognized agent-like mention.
    - ``{"action": "acknowledged", ...}`` when a fresh acknowledgement
      comment was posted.
    - ``{"action": "noop", ...}`` when a prior acknowledgement already
      exists for the same triggering comment.
    """
    pr_number = _resolve_pr_number(payload)
    if pr_number <= 0:
        return {"action": "skipped", "reason": "missing PR/issue number"}

    repo_payload = payload.get("repository") or {}
    full_name = str(repo_payload.get("full_name") or "")
    if "/" not in full_name:
        return {
            "action": "skipped",
            "reason": "missing repository.full_name",
            "pr_number": pr_number,
        }
    owner, repo = full_name.split("/", 1)

    trigger_comment_id, body = _resolve_trigger(payload)
    handle = find_unrecognized_agent_mention(body)
    if handle is None:
        return {
            "action": "skipped",
            "reason": "no unrecognized agent mention in trigger body",
            "pr_number": pr_number,
        }

    issue_handle = repo_handle.get_issue(int(pr_number))

    # Idempotency: skip the post when a prior acknowledgement already
    # exists for this triggering comment. The marker pins the dedupe to
    # this workflow + comment so retried deliveries do not double-post,
    # while a different unrecognized mention on the same PR still gets
    # its own acknowledgement.
    marker = _acknowledgement_metadata(pr_number, trigger_comment_id)
    if _existing_acknowledgement(issue_handle, marker=marker) is not None:
        return {
            "action": "noop",
            "reason": "acknowledgement already posted for this comment",
            "pr_number": pr_number,
            "mentioned_handle": handle,
        }

    comment_body = _build_acknowledgement_body(handle)
    try:
        issue_handle.create_comment(f"{comment_body}\n\n{marker}")
    except Exception:
        logger.exception(
            "Failed to post unknown-mention acknowledgement on PR #%s in %s/%s",
            pr_number,
            owner,
            repo,
        )
        return {
            "action": "skipped",
            "reason": "failed to post acknowledgement comment",
            "pr_number": pr_number,
            "mentioned_handle": handle,
        }

    return {
        "action": "acknowledged",
        "pr_number": pr_number,
        "mentioned_handle": handle,
    }


__all__ = [
    "OZ_AGENT_MENTION",
    "WARP_AGENT_MENTION",
    "WORKFLOW_NAME",
    "apply_acknowledge_unknown_mention_sync",
]
