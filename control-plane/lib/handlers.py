"""Concrete cron-side handlers for cloud-agent workflows.

The cron poller (see :mod:`api.cron`) drains in-flight runs from KV and
hands each one to the :class:`WorkflowHandlers` registered for its
workflow. This module wires up:

- ``artifact_loader``: a thin wrapper around the workflow-specific
  ``oz_workflows.artifacts.load_*_artifact`` helper.
- ``result_applier``: mints a fresh App-installation token from the
  saved ``installation_id``, builds a :class:`Github` client, and calls
  the workflow-specific ``apply_*_result`` helper.
- ``failure_handler``: posts a workflow-specific error message via
  :class:`WorkflowProgressComment`.

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


def build_review_handlers(github_client_factory: GithubClientFactory) -> WorkflowHandlers:
    """Return :class:`WorkflowHandlers` for ``review-pull-request``."""
    from oz_workflows.artifacts import load_review_artifact  # type: ignore[import-not-found]
    from oz_workflows.helpers import (  # type: ignore[import-not-found]
        WorkflowProgressComment,
    )
    from scripts.review_pr import apply_review_result  # type: ignore[import-not-found]

    def loader(run_id: str) -> dict[str, Any]:
        return load_review_artifact(run_id)

    def applier(*, state: RunState, result: Mapping[str, Any]) -> None:
        owner, repo = _resolve_owner_repo(state)
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        # The poller does not give us the original ``RunItem``; the
        # apply helpers only read ``run_id`` and ``session_link`` so an
        # adapter object suffices.
        run_adapter = type("CronRunAdapter", (), {"run_id": state.run_id, "session_link": ""})()
        try:
            apply_review_result(
                repo_handle,
                context=state.payload_subset,
                run=run_adapter,
                result=dict(result),
            )
        except Exception:
            _report_workflow_error(
                repo_handle,
                state=state,
                workflow=WORKFLOW_REVIEW_PR,
            )
            raise

    def failure(*, state: RunState, run: Any) -> None:
        owner, repo = _resolve_owner_repo(state)
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        _report_workflow_error(
            repo_handle,
            state=state,
            workflow=WORKFLOW_REVIEW_PR,
        )

    return WorkflowHandlers(
        artifact_loader=loader,
        result_applier=applier,
        failure_handler=failure,
    )


def build_respond_handlers(
    github_client_factory: GithubClientFactory,
) -> WorkflowHandlers:
    """Return :class:`WorkflowHandlers` for ``respond-to-pr-comment``."""
    from oz_workflows.helpers import WorkflowProgressComment  # type: ignore[import-not-found]
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
        owner, repo = _resolve_owner_repo(state)
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        run_adapter = type(
            "CronRunAdapter",
            (),
            {"run_id": state.run_id, "session_link": "", "created_at": None},
        )()
        try:
            apply_pr_comment_result(
                repo_handle,
                context=state.payload_subset,
                run=run_adapter,
                client=client,
            )
        except Exception:
            _report_workflow_error(
                repo_handle,
                state=state,
                workflow=WORKFLOW_RESPOND_TO_PR_COMMENT,
            )
            raise

    def failure(*, state: RunState, run: Any) -> None:
        owner, repo = _resolve_owner_repo(state)
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        _report_workflow_error(
            repo_handle,
            state=state,
            workflow=WORKFLOW_RESPOND_TO_PR_COMMENT,
        )

    return WorkflowHandlers(
        artifact_loader=loader,
        result_applier=applier,
        failure_handler=failure,
    )


def build_verify_handlers(
    github_client_factory: GithubClientFactory,
) -> WorkflowHandlers:
    """Return :class:`WorkflowHandlers` for ``verify-pr-comment``."""
    from oz_workflows.artifacts import (  # type: ignore[import-not-found]
        load_run_artifact,
    )
    from oz_workflows.helpers import WorkflowProgressComment  # type: ignore[import-not-found]
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
        owner, repo = _resolve_owner_repo(state)
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        # The /oz-verify path renders the report into the progress
        # comment as soon as the run terminates; other reviewer-useful
        # artifacts (screenshots, logs, …) are linked via session.
        run_adapter = type("CronRunAdapter", (), {"run_id": state.run_id, "session_link": "", "artifacts": None})()
        try:
            apply_verification_result(
                repo_handle,
                context=state.payload_subset,
                run=run_adapter,
                result=dict(result),
                artifacts=[],
            )
        except Exception:
            _report_workflow_error(
                repo_handle,
                state=state,
                workflow=WORKFLOW_VERIFY_PR_COMMENT,
            )
            raise

    def failure(*, state: RunState, run: Any) -> None:
        owner, repo = _resolve_owner_repo(state)
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        _report_workflow_error(
            repo_handle,
            state=state,
            workflow=WORKFLOW_VERIFY_PR_COMMENT,
        )

    return WorkflowHandlers(
        artifact_loader=loader,
        result_applier=applier,
        failure_handler=failure,
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
        owner, repo = _resolve_owner_repo(state)
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        run_adapter = type("CronRunAdapter", (), {"run_id": state.run_id, "session_link": ""})()
        try:
            apply_issue_association_result(
                repo_handle,
                context=state.payload_subset,
                run=run_adapter,
                result=dict(result),
            )
        except Exception:
            _report_workflow_error(
                repo_handle,
                state=state,
                workflow=WORKFLOW_ENFORCE_PR_ISSUE_STATE,
            )
            raise

    def failure(*, state: RunState, run: Any) -> None:
        owner, repo = _resolve_owner_repo(state)
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        _report_workflow_error(
            repo_handle,
            state=state,
            workflow=WORKFLOW_ENFORCE_PR_ISSUE_STATE,
        )

    return WorkflowHandlers(
        artifact_loader=loader,
        result_applier=applier,
        failure_handler=failure,
    )


def _report_workflow_error(repo_handle: Any, *, state: RunState, workflow: str) -> None:
    """Best-effort: report a workflow-error progress comment on the originating PR."""
    from oz_workflows.helpers import WorkflowProgressComment  # type: ignore[import-not-found]

    payload = state.payload_subset or {}
    pr_number = int(payload.get("pr_number") or 0)
    if pr_number <= 0:
        logger.warning(
            "Skipping report_workflow_error for run %s; payload_subset has no pr_number",
            state.run_id,
        )
        return
    owner, repo = _resolve_owner_repo(state)
    requester = str(payload.get("requester") or "")
    progress = WorkflowProgressComment(
        repo_handle,
        owner,
        repo,
        pr_number,
        workflow=workflow,
        requester_login=requester,
    )
    try:
        progress.report_error()
    except Exception:
        logger.exception(
            "Failed to post workflow error comment for %s on PR #%s in %s",
            workflow,
            pr_number,
            state.repo,
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
    }


__all__ = [
    "GithubClientFactory",
    "build_enforce_handlers",
    "build_handler_registry",
    "build_respond_handlers",
    "build_review_handlers",
    "build_verify_handlers",
]
