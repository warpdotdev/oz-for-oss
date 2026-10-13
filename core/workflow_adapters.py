from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Mapping

from oz.agent_workflow import (
    AgentWorkflow,
    WorkflowDispatch,
    create_progress_comment,
)

from .dispatch import DispatchRequest, PromptBuilder
from .poll_runs import WorkflowHandlers
from .state import RunState

logger = logging.getLogger(__name__)


GithubClientFactory = Callable[[int], Any]


def _client_factory(install_id: int, factory: GithubClientFactory) -> Any:
    if install_id <= 0:
        raise RuntimeError(
            "RunState.installation_id must be a positive integer; got "
            f"{install_id!r}"
        )
    return factory(install_id)


def _resolve_owner_repo(state: RunState) -> tuple[str, str]:
    if "/" not in state.repo:
        raise RuntimeError(
            f"RunState.repo {state.repo!r} is not an 'owner/repo' slug"
        )
    owner, repo = state.repo.split("/", 1)
    return owner, repo


def progress_issue_number(payload: Mapping[str, Any], *, run_id: str) -> int:
    issue_number_raw = payload.get("pr_number")
    if issue_number_raw in (None, 0, "0", ""):
        issue_number_raw = payload.get("issue_number")
    issue_number = int(issue_number_raw or 0)
    if issue_number <= 0:
        raise RuntimeError(
            f"RunState.payload_subset for run {run_id!r} is missing pr_number/issue_number"
        )
    return issue_number


def reconstruct_progress(
    repo_handle: Any,
    *,
    state: RunState,
    workflow: str,
    review_reply_target: tuple[Any, int] | None = None,
) -> Any:
    from oz.helpers import WorkflowProgressComment  # type: ignore[import-not-found]

    payload = state.payload_subset or {}
    issue_number = progress_issue_number(payload, run_id=state.run_id)
    owner, repo = _resolve_owner_repo(state)
    progress_comment_id = int(payload.get("progress_comment_id") or 0)
    return WorkflowProgressComment(
        repo_handle,
        owner,
        repo,
        issue_number,
        workflow=workflow,
        requester_login=str(payload.get("requester") or ""),
        review_reply_target=review_reply_target,
        comment_id=progress_comment_id or None,
        run_id=state.run_id,
    )


def record_session_link_safely(progress: Any, run: Any) -> None:
    from oz.helpers import record_run_session_link  # type: ignore[import-not-found]

    try:
        record_run_session_link(progress, run)
    except Exception:
        logger.exception(
            "record_run_session_link failed for progress comment on %s/%s issue #%s",
            getattr(progress, "owner", ""),
            getattr(progress, "repo", ""),
            getattr(progress, "issue_number", 0),
        )


def report_workflow_error_with_progress(progress: Any) -> None:
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


def dispatch_request_for_workflow(
    workflow: AgentWorkflow,
    payload: Mapping[str, Any],
    *,
    github_client: Any,
    workspace_path: Path | None = None,
) -> DispatchRequest | None:
    dispatch: WorkflowDispatch = workflow.build_dispatch(
        payload,
        github_client=github_client,
        workspace_path=workspace_path,
    )
    if dispatch is None:
        return None

    def on_dispatched(run_id: str) -> dict[str, Any]:
        progress = create_progress_comment(dispatch.progress, run_id=run_id)
        return {"progress_comment_id": int(getattr(progress, "comment_id", 0) or 0)}

    return DispatchRequest(
        workflow=dispatch.workflow,
        repo=dispatch.repo,
        installation_id=dispatch.installation_id,
        config_name=dispatch.config_name,
        title=dispatch.title,
        skill_name=dispatch.skill_name,
        prompt=dispatch.prompt,
        payload_subset=dict(dispatch.payload_subset),
        attachments=tuple(dispatch.attachments),
        on_dispatched=on_dispatched,
    )


def prompt_builder_for_workflow(
    workflow: AgentWorkflow,
    *,
    github_client_factory: Callable[[], Any],
    workspace_path: Path | None = None,
) -> PromptBuilder:
    def _adapter(payload: Mapping[str, Any]) -> DispatchRequest | None:
        return dispatch_request_for_workflow(
            workflow,
            payload,
            github_client=github_client_factory(),
            workspace_path=workspace_path,
        )

    return _adapter


def handlers_for_workflow(
    workflow: AgentWorkflow,
    *,
    github_client_factory: GithubClientFactory,
) -> WorkflowHandlers:
    def loader(run_id: str) -> dict[str, Any]:
        return workflow.load_artifact(run_id)

    def applier(*, state: RunState, result: Mapping[str, Any], run: Any | None = None) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = workflow.progress_for_state(repo_handle, state=state)
        # Record the Oz session link onto the progress comment before
        # applying the result. A run that reaches a terminal SUCCEEDED
        # state on the first cron poll never passes through
        # ``non_terminal_handler``, so without this the completion comment
        # posted back to the PR/issue -- including the "completed with no
        # work" message -- would omit the link back to the Warp
        # conversation that explains what the agent did. ``run_adapter``
        # derives its ``session_link`` from the progress comment, so this
        # must run before the adapter is built. Mirrors the failure
        # handler, which records the link before ``report_error``.
        if run is not None:
            record_session_link_safely(progress, run)
        run_adapter = workflow.run_adapter_for_state(state=state, progress=progress, run=run)
        try:
            workflow.apply_result(
                repo_handle,
                context=state.payload_subset,
                run=run_adapter,
                result=result,
                progress=progress,
                github_client=client,
            )
        except Exception:
            report_workflow_error_with_progress(progress)
            raise

    def failure(*, state: RunState, run: Any) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = workflow.progress_for_state(repo_handle, state=state)
        record_session_link_safely(progress, run)
        report_workflow_error_with_progress(progress)

    def non_terminal(*, state: RunState, run: Any) -> None:
        client = _client_factory(state.installation_id, github_client_factory)
        repo_handle = client.get_repo(state.repo)
        progress = workflow.progress_for_state(repo_handle, state=state)
        record_session_link_safely(progress, run)

    return WorkflowHandlers(
        artifact_loader=loader,
        result_applier=applier,
        failure_handler=failure,
        non_terminal_handler=non_terminal,
    )
