"""Helpers for resolving and referencing repo-local companion skills.

Core agent skills in ``.agents/skills/<agent>/SKILL.md`` express the stable
cross-repo contract. Companion skills in ``.agents/skills/<agent>-local/SKILL.md``
live in the consuming repository's checkout and specialize the override
categories the core skill explicitly allows. These helpers let prompt-
construction code resolve a companion file and embed a fenced section that
references (not inlines) the companion file when one exists.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from .workflow_config import SelfImprovementConfig, load_self_improvement_config


_FRONTMATTER_PATTERN = re.compile(
    r"\A\s*---\s*\n.*?\n---\s*\n?",
    re.DOTALL,
)


def _body_without_frontmatter(raw_text: str) -> str:
    """Return *raw_text* with an optional leading YAML frontmatter block removed."""
    return _FRONTMATTER_PATTERN.sub("", raw_text, count=1)


def resolve_repo_local_skill_path(
    workspace: Path, core_skill_name: str
) -> Path | None:
    """Resolve the repo-local companion skill path for *core_skill_name*.

    Returns the absolute path to ``.agents/skills/<core_skill_name>-local/SKILL.md``
    in the consuming repository's *workspace* when the file exists and contains
    non-frontmatter body content; otherwise returns ``None``.

    A missing file, an empty file, or a file that contains only YAML
    frontmatter (no body) is treated as absent so the caller can omit the
    companion reference entirely.
    """
    if not core_skill_name or not core_skill_name.strip():
        return None

    candidate = (
        Path(workspace)
        / ".agents"
        / "skills"
        / f"{core_skill_name}-local"
        / "SKILL.md"
    )
    try:
        if not candidate.is_file():
            return None
        raw_text = candidate.read_text(encoding="utf-8")
    except OSError:
        return None

    body = _body_without_frontmatter(raw_text).strip()
    if not body:
        return None
    return candidate.resolve()


def format_repo_local_prompt_section(
    core_skill_name: str, companion_path: Path
) -> str:
    """Return the fenced prompt section that references *companion_path*.

    The section intentionally contains only a path reference plus an
    override reminder. The companion body is never inlined into the prompt
    string; the agent is instructed to read the referenced file via its
    usual skill-read path.
    """
    return (
        f"## Repository-specific guidance for `{core_skill_name}`\n"
        f"Read and follow the companion skill at `{companion_path}` in the "
        "consuming repository's checkout. Its guidance may override only the "
        "categories your core skill marks as overridable. It must not change "
        "the core skill's output schema, severity labels, or safety rules.\n"
    )


# Write-surface guard used by the narrowed self-improvement loops.
#
# Each ``update-<agent>`` Python entrypoint runs ``git diff --name-only
# <base>...<branch>`` before pushing and passes the result to
# :func:`assert_write_surface` with the loop's allowed prefixes. Any file
# outside those prefixes aborts the run so the loop cannot silently expand
# its write surface into the core skill files or the workflow scripts.
class WriteSurfaceViolation(RuntimeError):
    """Raised when a self-improvement loop touched disallowed files."""


def assert_write_surface(
    changed_files: list[str],
    *,
    allowed_prefixes: list[str],
    loop_name: str,
) -> None:
    """Validate that every entry in *changed_files* starts with an allowed prefix.

    *allowed_prefixes* is a list of repository-root-relative prefixes
    (for example ``.agents/skills/review-pr-local/``). A file matches when
    its normalized path starts with any prefix.
    """
    normalized_prefixes = [p.replace("\\", "/") for p in allowed_prefixes if p]
    violations: list[str] = []
    for raw_path in changed_files:
        path = raw_path.strip()
        if not path:
            continue
        path = path.replace("\\", "/")
        if not any(path.startswith(prefix) for prefix in normalized_prefixes):
            violations.append(path)
    if violations:
        pretty = ", ".join(violations)
        allowed = ", ".join(normalized_prefixes) or "(none)"
        raise WriteSurfaceViolation(
            f"{loop_name} attempted to write outside its allowed surface. "
            f"Disallowed paths: {pretty}. Allowed prefixes: {allowed}."
        )


# Shared push/PR plumbing for the narrowed self-improvement loops.
#
# Each ``update-<agent>`` Python entrypoint invokes Oz, which leaves a
# local commit on ``oz-agent/update-<agent>`` without pushing. The
# entrypoint then calls :func:`maybe_push_update_branch` to run the
# write-surface guard, push the branch to ``origin`` only when the guard
# passes, and open a pull request so a human reviewer is notified.


def branch_exists(repo_root: Path, branch: str) -> bool:
    """Return ``True`` when ``refs/heads/<branch>`` exists under *repo_root*."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _remote_branch_exists(repo_root: Path, branch: str) -> bool:
    """Return ``True`` when *branch* exists on ``origin``."""
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _ensure_remote_tracking_branch(repo_root: Path, branch: str) -> None:
    """Ensure the ``origin/<branch>`` tracking ref exists locally."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    subprocess.run(
        ["git", "fetch", "origin", f"{branch}:refs/remotes/origin/{branch}"],
        cwd=str(repo_root),
        check=True,
    )


def changed_files_since_base_branch(
    repo_root: Path, branch: str, base_branch: str
) -> list[str]:
    """Return the list of paths changed on *branch* relative to ``origin/<base_branch>``."""
    _ensure_remote_tracking_branch(repo_root, base_branch)
    result = subprocess.run(
        ["git", "diff", "--name-only", f"origin/{base_branch}...{branch}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _detect_default_branch(repo_root: Path) -> str | None:
    symbolic_ref = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if symbolic_ref.returncode == 0:
        resolved = symbolic_ref.stdout.strip()
        if resolved.startswith("origin/"):
            return resolved.removeprefix("origin/")
        if resolved:
            return resolved

    remote_show = subprocess.run(
        ["git", "remote", "show", "origin"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if remote_show.returncode != 0:
        return None
    for line in remote_show.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("HEAD branch: "):
            branch = stripped.removeprefix("HEAD branch: ").strip()
            if branch:
                return branch
    return None


def resolve_self_improvement_base_branch(
    repo_root: Path, config: SelfImprovementConfig
) -> str:
    """Resolve the base branch for self-improvement runs."""
    if config.base_branch:
        if not _remote_branch_exists(repo_root, config.base_branch):
            raise RuntimeError(
                "Configured self-improvement base branch "
                f"{config.base_branch!r} does not exist on origin."
            )
        return config.base_branch

    detected = _detect_default_branch(repo_root)
    if not detected:
        raise RuntimeError(
            "Unable to detect the default branch for origin. Set "
            "SELF_IMPROVEMENT_BASE_BRANCH or self_improvement.base_branch in "
            ".github/oz/config.yml."
        )
    if not _remote_branch_exists(repo_root, detected):
        raise RuntimeError(
            f"Detected default branch {detected!r}, but it does not exist on origin."
        )
    return detected


def _normalize_repo_relative_path(raw_path: str) -> str:
    path = raw_path.strip().replace("\\", "/")
    if path.startswith("./"):
        return path[2:]
    return path


def _normalize_ownership_pattern(raw_pattern: str) -> str:
    return raw_pattern.strip().replace("\\", "/").lstrip("/")


def _pattern_matches(path: str, raw_pattern: str) -> bool:
    pattern = _normalize_ownership_pattern(raw_pattern)
    if not pattern:
        return False
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if "/" not in pattern:
        return fnmatch.fnmatch(path, f"*/{pattern}") or fnmatch.fnmatch(path, pattern)
    return PurePosixPath(path).match(pattern)


def _parse_ownership_rules(ownership_file: Path) -> list[tuple[str, list[str]]]:
    rules: list[tuple[str, list[str]]] = []
    for raw_line in ownership_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern, raw_owners = parts[0], parts[1:]
        owners = [owner.removeprefix("@") for owner in raw_owners if owner.startswith("@")]
        if owners:
            rules.append((pattern, owners))
    return rules


def _resolve_reviewers_from_ownership_file(
    ownership_file: Path, changed_files: list[str]
) -> list[str]:
    rules = _parse_ownership_rules(ownership_file)
    reviewers: list[str] = []
    seen: set[str] = set()
    for raw_path in changed_files:
        path = _normalize_repo_relative_path(raw_path)
        matched_owners: list[str] = []
        for pattern, owners in rules:
            if _pattern_matches(path, pattern):
                matched_owners = owners
        for owner in matched_owners:
            if owner not in seen:
                seen.add(owner)
                reviewers.append(owner)
    return reviewers


def resolve_self_improvement_reviewers(
    repo_root: Path,
    changed_files: list[str],
    config: SelfImprovementConfig,
) -> list[str]:
    """Resolve reviewer handles for self-improvement pull requests."""
    if config.reviewers is not None:
        return list(config.reviewers)

    ownership_candidates = [
        repo_root / ".github" / "STAKEHOLDERS",
        repo_root / ".github" / "CODEOWNERS",
        repo_root / "CODEOWNERS",
    ]
    for ownership_file in ownership_candidates:
        if ownership_file.is_file():
            return _resolve_reviewers_from_ownership_file(
                ownership_file, changed_files
            )
    return []


def _pr_exists_for_branch(repo_root: Path, branch: str) -> bool:
    """Return ``True`` when an open PR already targets *branch* as its head.

    Uses ``gh pr list --head`` which scopes to open PRs by default. Returns
    ``False`` on any gh/authentication error so the caller falls back to
    attempting ``gh pr create`` and surfaces any real failure from there.
    """
    result = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--json", "number"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    stdout = result.stdout.strip()
    # ``gh pr list --json number`` returns ``[]`` when there are no open PRs
    # against the head and a non-empty JSON array when there is at least one.
    return bool(stdout) and stdout != "[]"


def maybe_push_update_branch(
    repo_root: Path,
    branch: str,
    *,
    allowed_prefixes: list[str],
    loop_name: str,
    pr_title: str,
    pr_body: str,
    metadata_supplier: Callable[[], dict[str, Any]] | None = None,
) -> None:
    """Enforce the write surface, push *branch*, and open a PR if one is missing.

    *metadata_supplier* is an optional zero-argument callable that returns a
    dict with at least ``pr_title`` and ``pr_summary`` keys. When provided it
    is called only after confirming there are actual changed files to push, so
    callers can defer expensive artifact fetches to the moment they are
    genuinely needed.
    """
    if not branch_exists(repo_root, branch):
        return

    config = load_self_improvement_config(repo_root)
    base_branch = resolve_self_improvement_base_branch(repo_root, config)
    changed_files = changed_files_since_base_branch(repo_root, branch, base_branch)
    if not changed_files:
        return
    if metadata_supplier is not None:
        metadata = metadata_supplier()
        pr_title = metadata.get("pr_title", pr_title)
        pr_body = metadata.get("pr_summary", pr_body)
    assert_write_surface(
        changed_files,
        allowed_prefixes=allowed_prefixes,
        loop_name=loop_name,
    )
    reviewers = resolve_self_improvement_reviewers(repo_root, changed_files, config)
    subprocess.run(
        ["git", "push", "origin", branch],
        cwd=str(repo_root),
        check=True,
    )
    if _pr_exists_for_branch(repo_root, branch):
        return
    create_cmd = [
        "gh",
        "pr",
        "create",
        "--head",
        branch,
        "--base",
        base_branch,
        "--title",
        pr_title,
        "--body",
        pr_body,
    ]
    if reviewers:
        create_cmd.extend(["--reviewer", ",".join(reviewers)])
    subprocess.run(create_cmd, cwd=str(repo_root), check=True)
