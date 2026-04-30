from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class ProgressCommentSpec:
    """Information needed to create or reconstruct a workflow progress comment."""

    repo_handle: Any
    owner: str
    repo: str
    issue_number: int
    workflow: str
    start_line: str
    requester_login: str = ""
    event_payload: Mapping[str, Any] | None = None
    review_reply_target: tuple[Any, int] | None = None
    comment_id: int | None = None


@dataclass(frozen=True)
class WorkflowDispatch:
    """Workflow-specific data required to dispatch an Oz agent run."""

    workflow: str
    repo: str
    installation_id: int
    config_name: str
    title: str
    skill_name: str | None
    prompt: str
    payload_subset: dict[str, Any]
    progress: ProgressCommentSpec


class AgentWorkflow(Protocol):
    """Template-method contract implemented by each agent-backed workflow."""

    workflow: str
    config_name: str

    def build_dispatch(
        self,
        payload: Mapping[str, Any],
        *,
        github_client: Any,
        workspace_path: Any = None,
    ) -> WorkflowDispatch: ...

    def load_artifact(self, run_id: str) -> dict[str, Any]: ...

    def apply_result(
        self,
        repo_handle: Any,
        *,
        context: Mapping[str, Any],
        run: Any,
        result: Mapping[str, Any],
        progress: Any,
        github_client: Any | None = None,
    ) -> None: ...

    def progress_for_state(
        self,
        repo_handle: Any,
        *,
        state: Any,
    ) -> Any: ...

    def run_adapter_for_state(
        self,
        *,
        state: Any,
        progress: Any,
        run: Any | None = None,
    ) -> Any: ...


def create_progress_comment(spec: ProgressCommentSpec, *, run_id: str) -> Any:
    """Create the progress comment for a dispatched run using the Oz run id."""
    from oz.helpers import WorkflowProgressComment  # type: ignore[import-not-found]

    progress = WorkflowProgressComment(
        spec.repo_handle,
        spec.owner,
        spec.repo,
        spec.issue_number,
        workflow=spec.workflow,
        event_payload=dict(spec.event_payload or {}),
        requester_login=spec.requester_login,
        review_reply_target=spec.review_reply_target,
        comment_id=spec.comment_id,
        run_id=run_id,
    )
    if spec.start_line:
        progress.start(spec.start_line)
    elif spec.comment_id:
        progress.record_oz_run_id(run_id)
    return progress


def make_run_adapter(*, state: Any, progress: Any, run: Any | None = None) -> Any:
    """Return the minimal run object shape consumed by apply helpers."""
    created_at = getattr(run, "created_at", None) if run is not None else None
    if not isinstance(created_at, datetime):
        try:
            created_at = datetime.fromtimestamp(float(state.dispatched_at), timezone.utc)
        except (AttributeError, TypeError, ValueError, OSError):
            created_at = None
    return SimpleNamespace(
        run_id=state.run_id,
        session_link=getattr(progress, "session_link", ""),
        created_at=created_at,
        artifacts=getattr(run, "artifacts", None) if run is not None else None,
    )
