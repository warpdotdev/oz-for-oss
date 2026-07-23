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


# Recognized boolean flag values (compared case-insensitively). An unset or
# empty variable resolves to the helper's *default*, so opt-in flags stay off
# unless a caller explicitly enables them. Any non-empty value that is not a
# recognized truthy value is treated as falsy, keeping the safe default.
_TRUTHY_FLAG_VALUES = {"1", "true", "yes", "on"}


def flag_env(name: str, *, default: bool = False) -> bool:
    """Return an environment variable interpreted as a boolean flag.

    Truthy values (case-insensitive): ``1``, ``true``, ``yes``, ``on``.
    Any other non-empty value is treated as falsy. An unset or empty
    variable returns *default* (``False`` by default), so opt-in flags
    default off unless explicitly enabled.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return raw.lower() in _TRUTHY_FLAG_VALUES


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
    """Load the workflow event payload JSON."""
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

