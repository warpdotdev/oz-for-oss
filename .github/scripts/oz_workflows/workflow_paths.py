from __future__ import annotations

from pathlib import Path

from .env import workspace


def workflow_code_root(start_path: str | Path | None = None) -> Path:
    """Return the workflow code root by walking up to the nearest .github directory."""
    start = Path(start_path or __file__).resolve()
    for candidate in start.parents:
        if (candidate / ".github").is_dir():
            return candidate
    raise RuntimeError(
        "Unable to locate the workflow code root: no '.github' sentinel "
        f"directory found while walking up from {start}."
    )


def preferred_repo_roots(workspace_root: Path | None = None) -> list[Path]:
    """Return the consuming repo root first, then the workflow checkout root."""
    consumer_root = (workspace_root or workspace()).resolve()
    workflow_root = workflow_code_root().resolve()
    roots = [consumer_root]
    if workflow_root != consumer_root:
        roots.append(workflow_root)
    return roots
