"""Concrete cron-side handlers for cloud-agent workflows.

The cron poller (see :mod:`api.cron`) drains in-flight runs from KV and
hands each one to the :class:`WorkflowHandlers` registered for its
workflow. This module wires up:

- ``artifact_loader``: a thin wrapper around the workflow-specific
  ``oz_workflows.artifacts.load_*_artifact`` helper.
- ``result_applier``: mints a fresh App-installation token from the
  saved ``installation_id``, builds a :class:`Github` client,
  reconstructs the :class:`WorkflowProgressComment` posted at dispatch
  time, and calls the workflow-specific ``apply_*_result`` helper with
  it so the final ``progress.complete`` / ``progress.replace_body``
  edits land on the original comment.
- ``failure_handler``: rebuilds the same
  :class:`WorkflowProgressComment` and calls
  :meth:`WorkflowProgressComment.report_error` so the failure message
  replaces the in-flight progress comment.
- ``non_terminal_handler``: rebuilds the progress comment on each cron
  tick where the run is still pending and calls
  :func:`record_run_session_link` so the session-share link surfaces
  to viewers as soon as Oz reports it.

Each handler builds its GitHub client lazily inside the call so a
single failing workflow cannot poison the rest of the cron tick. All
exceptions raised by the apply step are propagated up to the poller,
which already wraps the call in a structured ``try/except`` and bumps
the retry counter on the in-flight record.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from .poll_runs import WorkflowHandlers
from .routing import (
    WORKFLOW_ENFORCE_PR_ISSUE_STATE,
    WORKFLOW_RESPOND_TO_PR_COMMENT,
    WORKFLOW_REVIEW_PR,
    WORKFLOW_TRIAGE_NEW_ISSUES,
    WORKFLOW_VERIFY_PR_COMMENT,
)
from .state import RunState

logger = logging.getLogger(__name__)


GithubClientFactory = Callable[[int], Any]
"""Callable that takes an installation id and returns a PyGithub client."""


def _resolve_owner_repo(state: RunState) -> tuple[str, str]:
    if "/" not in state.repo:
        raise RuntimeError(
            f"RunState.repo {state.repo!r} is not an 'owner/repo' slug"
        )
    owner, repo = state.repo.split("/", 1)
    return owner, repo


def _client_factory(install_id: int, factory: GithubClientFactory) -> Any:
    if install_id <= 0:
        raise RuntimeError(
            "RunState.installation_id must be a positive integer; got "
            f"{install_id!r}"
        )
    return factory(install_id)


def _reconstruct_progress(
    repo_handle: Any,
    *,
    state: RunState,
    workflow: str,
    review_reply_target: tuple[Any, int] | None = None,
) -> Any:
    """Rebuild the :class:`WorkflowProgressComment` posted at dispatch.

    The Vercel webhook stashes ``progress_comment_id`` and
    ``progress_run_id`` on ``state.payload_subset`` so the cron poller
    can edit the same GitHub comment posted by the builder. The
    fallback path (no stashed id) still works because
    :class:`WorkflowProgressComment` falls back to a workflow-prefix
    lookup when the ``GITHUB_RUN_ID`` is empty.
    """
    from oz_workflows.helpers import (  # type: ignore[import-not-found]
        WorkflowProgressComment,
    )

    payload = state.payload_subset or {}
    issue_number_raw = payload.get("pr_number")
    if issue_number_raw in (None, 0, "0", ""):
        # Triage payloads carry ``issue_number`` instead of ``pr_number``.
        issue_number_raw = payload.get("issue_number")
    issue_number = int(issue_number_raw or 0)
    if issue_number <= 0:
        raise RuntimeError(
            f"RunState.payload_subset for run {state.run_id!r} is missing pr_number/issue_number"
        )
    owner, repo = _resolve_owner_repo(state)
    progress_comment_id = int(payload.get("progress_comment_id") or 0)
    progress_run_id = str(payload.get("progress_run_id") or "")
    return WorkflowProgressComment(
        repo_handle,
        owner,
        repo,
        issue_number,
        workflow=workflow,
        requester_login=str(payload.get("requester") or ""),
        review_reply_target=review_reply_target,
        comment_id=progress_comment_id or None,
        run_id=progress_run_id or None,
    )


def _record_session_link_safely(progress: Any, run: Any) -> None:
    """Wrap :func:`record_run_session_link` so per-run failures stay local.

    The cron poller already absorbs exceptions raised by the
    ``non_terminal_handler``, but ``record_run_session_link`` itself is
    intentionally tolerant of transient GitHub failures (it logs and
    moves on). This wrapper keeps the contract identical between the
    GHA ``on_poll`` path and the cron drain path.
    """
    from oz_workflows.helpers import (  # type: ignore[import-not-found]
        record_run_session_link,
    )

    try:
        record_run_session_link(progress, run)
    except Exception:
        logger.exception(
            "record_run_session_link failed for progress comment on %s/%s issue #%s",
            getattr(progress, "owner", ""),
            getattr(progress, "repo", ""),
            getattr(progress, "issue_number", 0),
        )


def _resolve_review_reply_target_for_state(
    state: RunState, repo_handle: Any
) -> tuple[Any, int] | None:
    """Reconstruct the review-thread reply target stashed by the builder.

    The respond-to-pr-comment builder runs against an inline review
    comment and stashes the trigger comment id under
    ``review_reply_target_id``. The cron poller can rebuild the
    ``WorkflowProgressComment`` against the same review thread by
    pairing that id with the PR handle. ``None`` falls back to issue-
    level commenting.
    """
    payload = state.payload_subset or {}
    review_reply_target_id = int(payload.get("review_reply_target_id") or 0)
    if review_reply_target_id <= 0:
        return None
    pr_number = int(payload.get("pr_number") or 0)
    if pr_number <= 0:
        return None
    pr = repo_handle.get_pull(pr_number)
    return (pr, review_reply_target_id)


def _report_workflow_error_with_progress(progress: Any) -> None:
    """Wrap :meth:`WorkflowProgressComment.report_error` and absorb errors."""
    try:
        progress.report_error()
    except Exception:
        logger.exception(
            "Failed to update workflow error comment for %s on issue #%s in %s/%s",
            getattr(progress, "workflow", ""),
            getattr(progress, "issue_number", 0),
            getattr(progress, "owner", ""),
            getattr(progress, "repo", ""),
        )


def build_review_handlers(github_client_factory: GithubClientFactory) -> WorkflowHandlers:
    """Return :class:`WorkflowHandlers` for ``review-pull-request``."""
    from oz_workflows.artifacts import load_review_artifact  # type: ignore[import-not-found]
    from scripts.review_pr import apply_review_result  # type: ignore[import-not-found]

    def loader(run_id: str) -> dict[str, Any]:
        return load_review_artifact(run_id)

    def applier(*, state: RunState, result: Mapping[str, Any]) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = _reconstruct_progress(
            repo_handle, state=state, workflow=WORKFLOW_REVIEW_PR
        )
        # The poller does not give us the original ``RunItem``; the
        # apply helpers only read ``run_id`` and ``session_link`` so an
        # adapter object suffices.
        run_adapter = type(
            "CronRunAdapter",
            (),
            {"run_id": state.run_id, "session_link": progress.session_link},
        )()
        try:
            apply_review_result(
                repo_handle,
                context=state.payload_subset,
                run=run_adapter,
                result=dict(result),
                progress=progress,
            )
        except Exception:
            _report_workflow_error_with_progress(progress)
            raise

    def failure(*, state: RunState, run: Any) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = _reconstruct_progress(
            repo_handle, state=state, workflow=WORKFLOW_REVIEW_PR
        )
        _record_session_link_safely(progress, run)
        _report_workflow_error_with_progress(progress)

    def non_terminal(*, state: RunState, run: Any) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = _reconstruct_progress(
            repo_handle, state=state, workflow=WORKFLOW_REVIEW_PR
        )
        _record_session_link_safely(progress, run)

    return WorkflowHandlers(
        artifact_loader=loader,
        result_applier=applier,
        failure_handler=failure,
        non_terminal_handler=non_terminal,
    )


def build_respond_handlers(
    github_client_factory: GithubClientFactory,
) -> WorkflowHandlers:
    """Return :class:`WorkflowHandlers` for ``respond-to-pr-comment``."""
    from scripts.respond_to_pr_comment import (  # type: ignore[import-not-found]
        apply_pr_comment_result,
    )

    def loader(run_id: str) -> dict[str, Any]:
        # The respond-to-pr-comment workflow does not produce a single
        # canonical artifact; the apply step pulls ``pr-metadata.json``
        # and ``resolved_review_comments.json`` itself, both of which
        # are optional. Returning an empty dict lets the poller's
        # ``result_applier`` continue without forcing a load.
        return {}

    def applier(*, state: RunState, result: Mapping[str, Any]) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        review_reply_target = _resolve_review_reply_target_for_state(
            state, repo_handle
        )
        progress = _reconstruct_progress(
            repo_handle,
            state=state,
            workflow=WORKFLOW_RESPOND_TO_PR_COMMENT,
            review_reply_target=review_reply_target,
        )
        run_adapter = type(
            "CronRunAdapter",
            (),
            {
                "run_id": state.run_id,
                "session_link": progress.session_link,
                "created_at": None,
            },
        )()
        try:
            apply_pr_comment_result(
                repo_handle,
                context=state.payload_subset,
                run=run_adapter,
                client=client,
                progress=progress,
            )
        except Exception:
            _report_workflow_error_with_progress(progress)
            raise

    def failure(*, state: RunState, run: Any) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        review_reply_target = _resolve_review_reply_target_for_state(
            state, repo_handle
        )
        progress = _reconstruct_progress(
            repo_handle,
            state=state,
            workflow=WORKFLOW_RESPOND_TO_PR_COMMENT,
            review_reply_target=review_reply_target,
        )
        _record_session_link_safely(progress, run)
        _report_workflow_error_with_progress(progress)

    def non_terminal(*, state: RunState, run: Any) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        review_reply_target = _resolve_review_reply_target_for_state(
            state, repo_handle
        )
        progress = _reconstruct_progress(
            repo_handle,
            state=state,
            workflow=WORKFLOW_RESPOND_TO_PR_COMMENT,
            review_reply_target=review_reply_target,
        )
        _record_session_link_safely(progress, run)

    return WorkflowHandlers(
        artifact_loader=loader,
        result_applier=applier,
        failure_handler=failure,
        non_terminal_handler=non_terminal,
    )


def build_verify_handlers(
    github_client_factory: GithubClientFactory,
) -> WorkflowHandlers:
    """Return :class:`WorkflowHandlers` for ``verify-pr-comment``."""
    from oz_workflows.artifacts import (  # type: ignore[import-not-found]
        load_run_artifact,
    )
    from oz_workflows.verification import (  # type: ignore[import-not-found]
        list_downloadable_verification_artifacts,
    )
    from scripts.verify_pr_comment import (  # type: ignore[import-not-found]
        VERIFICATION_REPORT_FILENAME,
        apply_verification_result,
    )

    def loader(run_id: str) -> dict[str, Any]:
        return load_run_artifact(run_id, filename=VERIFICATION_REPORT_FILENAME)

    def applier(*, state: RunState, result: Mapping[str, Any]) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = _reconstruct_progress(
            repo_handle, state=state, workflow=WORKFLOW_VERIFY_PR_COMMENT
        )
        # The /oz-verify path renders the report into the progress
        # comment as soon as the run terminates; other reviewer-useful
        # artifacts (screenshots, logs, …) are linked via session.
        run_adapter = type(
            "CronRunAdapter",
            (),
            {
                "run_id": state.run_id,
                "session_link": progress.session_link,
                "artifacts": None,
            },
        )()
        try:
            apply_verification_result(
                repo_handle,
                context=state.payload_subset,
                run=run_adapter,
                result=dict(result),
                artifacts=[],
                progress=progress,
            )
        except Exception:
            _report_workflow_error_with_progress(progress)
            raise

    def failure(*, state: RunState, run: Any) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = _reconstruct_progress(
            repo_handle, state=state, workflow=WORKFLOW_VERIFY_PR_COMMENT
        )
        _record_session_link_safely(progress, run)
        _report_workflow_error_with_progress(progress)

    def non_terminal(*, state: RunState, run: Any) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = _reconstruct_progress(
            repo_handle, state=state, workflow=WORKFLOW_VERIFY_PR_COMMENT
        )
        _record_session_link_safely(progress, run)

    return WorkflowHandlers(
        artifact_loader=loader,
        result_applier=applier,
        failure_handler=failure,
        non_terminal_handler=non_terminal,
    )


def build_enforce_handlers(
    github_client_factory: GithubClientFactory,
) -> WorkflowHandlers:
    """Return :class:`WorkflowHandlers` for ``enforce-pr-issue-state``."""
    from oz_workflows.artifacts import poll_for_artifact  # type: ignore[import-not-found]
    from scripts.enforce_pr_issue_state import (  # type: ignore[import-not-found]
        apply_issue_association_result,
    )

    def loader(run_id: str) -> dict[str, Any]:
        return poll_for_artifact(run_id, filename="issue_association.json")

    def applier(*, state: RunState, result: Mapping[str, Any]) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = _reconstruct_progress(
            repo_handle, state=state, workflow=WORKFLOW_ENFORCE_PR_ISSUE_STATE
        )
        run_adapter = type(
            "CronRunAdapter",
            (),
            {"run_id": state.run_id, "session_link": progress.session_link},
        )()
        try:
            apply_issue_association_result(
                repo_handle,
                context=state.payload_subset,
                run=run_adapter,
                result=dict(result),
                progress=progress,
            )
        except Exception:
            _report_workflow_error_with_progress(progress)
            raise

    def failure(*, state: RunState, run: Any) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = _reconstruct_progress(
            repo_handle, state=state, workflow=WORKFLOW_ENFORCE_PR_ISSUE_STATE
        )
        _record_session_link_safely(progress, run)
        _report_workflow_error_with_progress(progress)

    def non_terminal(*, state: RunState, run: Any) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = _reconstruct_progress(
            repo_handle, state=state, workflow=WORKFLOW_ENFORCE_PR_ISSUE_STATE
        )
        _record_session_link_safely(progress, run)

    return WorkflowHandlers(
        artifact_loader=loader,
        result_applier=applier,
        failure_handler=failure,
        non_terminal_handler=non_terminal,
    )


def build_triage_handlers(
    github_client_factory: GithubClientFactory,
) -> WorkflowHandlers:
    """Return :class:`WorkflowHandlers` for ``triage-new-issues``."""
    from oz_workflows.artifacts import load_triage_artifact  # type: ignore[import-not-found]
    from scripts.triage_new_issues import (  # type: ignore[import-not-found]
        apply_triage_result_for_dispatch,
    )

    def loader(run_id: str) -> dict[str, Any]:
        return load_triage_artifact(run_id)

    def applier(*, state: RunState, result: Mapping[str, Any]) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = _reconstruct_progress(
            repo_handle, state=state, workflow=WORKFLOW_TRIAGE_NEW_ISSUES
        )
        run_adapter = type(
            "CronRunAdapter",
            (),
            {"run_id": state.run_id, "session_link": progress.session_link},
        )()
        try:
            apply_triage_result_for_dispatch(
                repo_handle,
                context=state.payload_subset,
                run=run_adapter,
                result=dict(result),
                progress=progress,
            )
        except Exception:
            _report_workflow_error_with_progress(progress)
            raise

    def failure(*, state: RunState, run: Any) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = _reconstruct_progress(
            repo_handle, state=state, workflow=WORKFLOW_TRIAGE_NEW_ISSUES
        )
        _record_session_link_safely(progress, run)
        _report_workflow_error_with_progress(progress)

    def non_terminal(*, state: RunState, run: Any) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = _reconstruct_progress(
            repo_handle, state=state, workflow=WORKFLOW_TRIAGE_NEW_ISSUES
        )
        _record_session_link_safely(progress, run)

    return WorkflowHandlers(
        artifact_loader=loader,
        result_applier=applier,
        failure_handler=failure,
        non_terminal_handler=non_terminal,
    )


def build_handler_registry(
    *,
    github_client_factory: GithubClientFactory,
) -> Mapping[str, WorkflowHandlers]:
    """Return the cron-side handler registry keyed by workflow name."""
    return {
        WORKFLOW_REVIEW_PR: build_review_handlers(github_client_factory),
        WORKFLOW_RESPOND_TO_PR_COMMENT: build_respond_handlers(
            github_client_factory
        ),
        WORKFLOW_VERIFY_PR_COMMENT: build_verify_handlers(
            github_client_factory
        ),
        WORKFLOW_ENFORCE_PR_ISSUE_STATE: build_enforce_handlers(
            github_client_factory
        ),
        WORKFLOW_TRIAGE_NEW_ISSUES: build_triage_handlers(github_client_factory),
    }


__all__ = [
    "GithubClientFactory",
    "build_enforce_handlers",
    "build_handler_registry",
    "build_respond_handlers",
    "build_review_handlers",
    "build_triage_handlers",
    "build_verify_handlers",
]
