"""Synchronous + dispatch helpers for the ``plan-approved`` PR-label workflow.

The webhook handler invokes :func:`apply_plan_approved_sync` whenever a
``pull_request.labeled`` delivery carries the ``plan-approved`` label.
The helper:

1. Confirms the PR is open and is a spec PR (head branch matches the
   ``oz-agent/spec-issue-{N}`` pattern, or every changed file lives
   under ``specs/``).
2. Resolves the linked issue via ``resolve_issue_number_for_pr``.
3. Posts the "spec approved in PR #N" comment on the linked issue
   exactly once (idempotency is enforced via a workflow-prefix
   metadata marker so retried webhook deliveries do not double-post).
4. Removes the ``ready-to-spec`` label from the linked issue if
   present.
5. Decides whether the linked issue is ready for implementation
   (``ready-to-implement`` plus an ``oz-agent`` assignee) and either
   returns a structured outcome (no implementation needed) or returns
   ``None`` to signal "fall through to the cloud-agent dispatch path".

The dispatch path reuses :func:`build_create_implementation_request`
under the ``WORKFLOW_PLAN_APPROVED`` workflow string so the cron poller
can apply the agent's ``pr-metadata.json`` artifact via the existing
implementation helpers.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from github.Repository import Repository

from oz.helpers import (
    _workflow_metadata_prefix,
    comment_metadata,
    is_spec_only_pr,
    resolve_issue_number_for_pr,
)

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "plan-approved"
PLAN_APPROVED_LABEL = "plan-approved"
READY_TO_SPEC_LABEL = "ready-to-spec"
READY_TO_IMPLEMENT_LABEL = "ready-to-implement"
OZ_AGENT_LOGIN = "oz-agent"

_SPEC_BRANCH_PATTERN = re.compile(r"(?:^|/)spec-issue-\d+(?:$|[/-])")


def _is_spec_pr(pr_obj: Any, changed_files: list[str]) -> bool:
    """Return True when *pr_obj* looks like a spec PR.

    A PR qualifies when its head branch matches the agent's
    ``oz-agent/spec-issue-{N}`` naming convention OR every changed
    file lives under ``specs/``. This keeps the plan-approved flow
    from firing on arbitrary non-spec PRs that merely reference an
    issue in their body.
    """
    head_ref = ""
    try:
        head_ref = str(pr_obj.head.ref or "")
    except AttributeError:
        head_ref = ""
    if head_ref and _SPEC_BRANCH_PATTERN.search(head_ref):
        return True
    return is_spec_only_pr(changed_files)


def _build_plan_approved_comment(*, owner: str, repo: str, pr_number: int) -> str:
    """Return the spec-approved comment body posted on the linked issue.

    Wording is kept stable so in-flight ``plan-approved`` PRs see
    consistent notifications across deployments.
    """
    pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
    return (
        f"A spec for this issue has been approved in [PR #{pr_number}]({pr_url}). "
        "Future implementations of this issue should use the approved spec as the "
        "basis for implementation."
    )


def _existing_plan_approved_comment(
    issue_handle: Any, *, prefix: str
) -> Any | None:
    """Return the prior plan-approved comment posted on *issue_handle*, if any.

    Idempotency is enforced via a workflow-scoped metadata marker:
    each posted comment ends with ``<!-- oz-agent-metadata: ... -->``
    whose JSON payload includes the workflow name and issue number.
    GitHub retries failed webhook deliveries, so the helper has to
    detect "we already commented on this PR's issue" without scanning
    every comment body for the natural-language wording.
    """
    try:
        comments = list(issue_handle.get_comments())
    except Exception:
        logger.exception(
            "Failed to list comments on issue while deduping plan-approved post"
        )
        return None
    for comment in comments:
        body = str(getattr(comment, "body", "") or "")
        if prefix in body:
            return comment
    return None


def apply_plan_approved_sync(
    repo_handle: Repository,
    *,
    payload: Mapping[str, Any],
    github_client: Any | None = None,
) -> dict[str, Any] | None:
    """Run the synchronous side effects for a ``plan-approved`` PR label.

    Returns:

    - ``{"action": "skipped", "reason": ...}`` when the event does not
      qualify (PR closed, non-spec PR, no linked issue).
    - ``{"action": "synced", ...}`` when the comment + label-removal
      side effects ran but the linked issue is not ready for
      implementation. The webhook handler returns 202 inline with this
      outcome.
    - ``None`` when the linked issue IS ready for implementation. The
      sync side effects still run; the webhook handler then falls
      through to the cloud-agent dispatch path. The resolved issue
      number is stashed onto the (mutable) ``payload`` dict under
      ``linked_issue_number`` so the dispatch builder reuses the
      lookup instead of re-resolving from scratch.
    """
    pr_payload = payload.get("pull_request") or {}
    if not isinstance(pr_payload, dict):
        return {"action": "skipped", "reason": "missing pull_request payload"}
    pr_number = int(pr_payload.get("number") or 0)
    if pr_number <= 0:
        return {"action": "skipped", "reason": "missing pr_number"}
    if str(pr_payload.get("state") or "") != "open":
        return {"action": "skipped", "reason": "PR is not open"}
    repo_payload = payload.get("repository") or {}
    full_name = str(repo_payload.get("full_name") or "")
    if "/" not in full_name:
        return {"action": "skipped", "reason": "missing repository.full_name"}
    owner, repo = full_name.split("/", 1)

    pr_obj = repo_handle.get_pull(pr_number)
    files = list(pr_obj.get_files())
    changed_files = [str(f.filename) for f in files]

    if not _is_spec_pr(pr_obj, changed_files):
        return {
            "action": "skipped",
            "reason": "PR is not a spec PR",
            "pr_number": pr_number,
        }

    issue_number = resolve_issue_number_for_pr(
        repo_handle, owner, repo, pr_obj, changed_files
    )
    if not issue_number:
        return {
            "action": "skipped",
            "reason": "no linked issue resolvable for PR",
            "pr_number": pr_number,
        }

    issue_handle = repo_handle.get_issue(int(issue_number))

    # Idempotency: skip the comment post when a prior plan-approved
    # comment for this issue already exists. We check the metadata
    # prefix rather than the body wording so prefix changes (e.g.
    # adding a session link) do not break the dedupe.
    metadata_prefix = _workflow_metadata_prefix(WORKFLOW_NAME, int(issue_number))
    existing_comment = _existing_plan_approved_comment(
        issue_handle, prefix=metadata_prefix
    )
    comment_posted = False
    if existing_comment is None:
        body = _build_plan_approved_comment(
            owner=owner, repo=repo, pr_number=pr_number
        )
        metadata = comment_metadata(WORKFLOW_NAME, int(issue_number))
        try:
            issue_handle.create_comment(f"{body}\n\n{metadata}")
            comment_posted = True
        except Exception:
            logger.exception(
                "Failed to post plan-approved comment on issue #%s", issue_number
            )

    # Remove the ``ready-to-spec`` label so the issue's lifecycle
    # advances.
    label_names = {
        str(getattr(label, "name", "") or "")
        for label in (issue_handle.labels or [])
    }
    label_removed = False
    if READY_TO_SPEC_LABEL in label_names:
        try:
            issue_handle.remove_from_labels(READY_TO_SPEC_LABEL)
            label_removed = True
        except Exception:
            logger.exception(
                "Failed to remove %r label from issue #%s",
                READY_TO_SPEC_LABEL,
                issue_number,
            )

    # Decide whether implementation should be triggered. The label
    # set after the removal above (``ready-to-spec`` may have just
    # been stripped) determines whether the issue is ready for
    # implementation.
    remaining_label_names = label_names - {READY_TO_SPEC_LABEL}
    assignee_logins = {
        str(getattr(assignee, "login", "") or "")
        for assignee in (issue_handle.assignees or [])
    }
    implementation_pending = (
        READY_TO_IMPLEMENT_LABEL in remaining_label_names
        and OZ_AGENT_LOGIN in assignee_logins
    )

    # Stash the resolved issue number so the dispatch builder can
    # reuse it without re-resolving the PR association from the API.
    if isinstance(payload, dict):
        payload["linked_issue_number"] = int(issue_number)

    if implementation_pending:
        # Falling through to the cloud-agent dispatch path. The sync
        # side effects above still ran.
        return None

    return {
        "action": "synced",
        "pr_number": pr_number,
        "linked_issue_number": int(issue_number),
        "comment_posted": comment_posted,
        "label_removed": label_removed,
        "implementation_triggered": False,
    }


__all__ = [
    "OZ_AGENT_LOGIN",
    "PLAN_APPROVED_LABEL",
    "READY_TO_IMPLEMENT_LABEL",
    "READY_TO_SPEC_LABEL",
    "WORKFLOW_NAME",
    "apply_plan_approved_sync",
]
