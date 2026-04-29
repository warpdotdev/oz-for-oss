from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def require_env(name: str) -> str:
    """Return a required environment variable after trimming surrounding whitespace."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def optional_env(name: str) -> str:
    """Return an optional environment variable as a trimmed string."""
    return os.environ.get(name, "").strip()


def repo_slug() -> str:
    """Return the current GitHub repository slug."""
    return require_env("GITHUB_REPOSITORY")


def repo_parts() -> tuple[str, str]:
    """Split the current repository slug into owner and repository name."""
    owner, repo = repo_slug().split("/", 1)
    return owner, repo


def workspace() -> Path:
    """Return the workflow workspace directory."""
    return Path(os.environ.get("GITHUB_WORKSPACE") or os.getcwd())


def load_event() -> dict[str, Any]:
    """Load the GitHub Actions event payload JSON."""
    event_path = require_env("GITHUB_EVENT_PATH")
    with open(event_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_issue_number(event: dict[str, Any], *, env_var: str = "ISSUE_NUMBER") -> int:
    """Resolve an issue number from the event payload or a workflow input env var."""
    issue_number = (event.get("issue") or {}).get("number")
    if issue_number not in (None, ""):
        return int(issue_number)
    override = optional_env(env_var)
    if override:
        return int(override)
    raise RuntimeError(
        f"Unable to resolve issue number from event payload or ${env_var}."
    )

