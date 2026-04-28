from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, cast

from oz_agent_sdk import OzAPI
from oz_agent_sdk.types import AgentRunParams, AmbientAgentConfigParam
from oz_agent_sdk.types.agent import RunItem

from .actions import notice, warning
from .env import optional_env, repo_slug, require_env, workspace
from .workflow_paths import workflow_code_root


TERMINAL_STATES = {"SUCCEEDED", "FAILED", "ERROR", "CANCELLED"}

# Default public-access level for the run's shared session.
#
# The oz-for-oss workflows are intended for OSS repositories where community
# members should be able to view agent activity through the session link
# without being invited to the run's team. We therefore opt every run into
# anyone-with-link viewer access by default. Callers that want to opt out or
# pick a different level can override this via the
# WARP_SESSION_SHARING_PUBLIC_ACCESS environment variable; set it to "NONE" or
# "OFF" to disable public sharing for a run.
DEFAULT_SESSION_SHARING_PUBLIC_ACCESS = "VIEWER"
_SESSION_SHARING_DISABLED_VALUES = {"NONE", "OFF", "DISABLED", "FALSE", "0"}
_SESSION_SHARING_SUPPORTED_LEVELS = {"VIEWER", "EDITOR"}


def oz_api_base_url() -> str:
    """Return the configured Oz API base URL.

    Callers must explicitly set ``WARP_API_BASE_URL`` so that every workflow
    declares which Oz environment it targets. This avoids silently running
    against an unexpected environment when the variable is forgotten, which
    is especially important for forks of this OSS template repository.
    """
    return require_env("WARP_API_BASE_URL")


def build_oz_client() -> OzAPI:
    """Build an authenticated Oz SDK client for GitHub Actions workflows."""
    return OzAPI(
        api_key=require_env("WARP_API_KEY"),
        base_url=oz_api_base_url(),
        default_headers={
            "x-oz-api-source": "GITHUB_ACTION",
        },
    )


def _resolve_session_sharing_public_access() -> str | None:
    """Resolve the configured public-access level for session sharing.

    Returns the level string (``"VIEWER"`` or ``"EDITOR"``) when public session
    sharing should be enabled for the run, or ``None`` when the caller has
    explicitly disabled public sharing.
    """
    raw = optional_env("WARP_SESSION_SHARING_PUBLIC_ACCESS")
    if raw == "":
        return DEFAULT_SESSION_SHARING_PUBLIC_ACCESS
    normalized = raw.upper()
    if normalized in _SESSION_SHARING_DISABLED_VALUES:
        return None
    if normalized not in _SESSION_SHARING_SUPPORTED_LEVELS:
        warning(
            "WARP_SESSION_SHARING_PUBLIC_ACCESS="
            f"{raw!r} is not a supported value; expected one of "
            f"{sorted(_SESSION_SHARING_SUPPORTED_LEVELS)} or a disable value "
            f"({sorted(_SESSION_SHARING_DISABLED_VALUES - {''})}). "
            "Disabling public session sharing for this run."
        )
        return None
    return normalized


# Roles understood by ``build_agent_config``. The role decides which
# environment-id env var is consulted first when picking a cloud
# environment for the run. ``"review-triage"`` covers the workflows that
# share the dedicated review/triage environment (PR review, issue
# triage, respond-to-triaged-issue-comment); every other workflow keeps
# using ``WARP_ENVIRONMENT_ID`` directly.
ROLE_REVIEW_TRIAGE = "review-triage"
ROLE_DEFAULT = "default"
_KNOWN_ROLES = {ROLE_DEFAULT, ROLE_REVIEW_TRIAGE}


def _resolve_environment_id(role: str) -> str:
    """Pick the Oz cloud environment id for *role*.

    For ``review-triage`` callers the operator may set
    ``WARP_REVIEW_TRIAGE_ENVIRONMENT_ID`` to point those workflows at a
    dedicated environment (typically tighter resource limits); when that
    variable is empty we fall back to ``WARP_ENVIRONMENT_ID`` so the
    deployment behaves the same as the legacy single-environment setup.
    Every other role reads ``WARP_ENVIRONMENT_ID`` directly.
    """
    if role == ROLE_REVIEW_TRIAGE:
        review_triage_env = optional_env("WARP_REVIEW_TRIAGE_ENVIRONMENT_ID")
        if review_triage_env:
            return review_triage_env
    return optional_env("WARP_ENVIRONMENT_ID")


def build_agent_config(
    *,
    config_name: str,
    workspace: Path,
    role: str = ROLE_DEFAULT,
) -> AmbientAgentConfigParam:
    """Build the agent configuration payload sent to the Oz API.

    *role* selects which environment-id env var is consulted. Pass
    ``ROLE_REVIEW_TRIAGE`` for the review/triage agents so the operator
    can route them onto ``WARP_REVIEW_TRIAGE_ENVIRONMENT_ID`` when
    configured. Unknown role values fall back to the default lookup
    rather than raising so future workflow additions don't have to
    coordinate a corresponding update here before they ship.
    """
    environment_id = _resolve_environment_id(role)
    if not environment_id:
        if role == ROLE_REVIEW_TRIAGE:
            raise RuntimeError(
                "Missing required Oz environment configuration. Set "
                "WARP_REVIEW_TRIAGE_ENVIRONMENT_ID (preferred) or "
                "WARP_ENVIRONMENT_ID to your Oz cloud environment UID "
                "(find it with `oz environment list` or in the Oz web app)."
            )
        raise RuntimeError(
            "Missing required Oz environment configuration. Set "
            "WARP_ENVIRONMENT_ID to your Oz cloud environment UID "
            "(find it with `oz environment list` or in the Oz web app)."
        )
    if role not in _KNOWN_ROLES:
        # Don't fail closed on an unrecognized role — log a warning so
        # operators can spot a typo, and proceed with the default
        # lookup that already produced ``environment_id``.
        warning(f"Unknown build_agent_config role {role!r}; falling back to {ROLE_DEFAULT!r}.")

    config: AmbientAgentConfigParam = {
        "environment_id": environment_id,
        "name": config_name,
    }
    model_id = optional_env("WARP_AGENT_MODEL")
    if model_id:
        config["model_id"] = model_id


    profile = optional_env("WARP_AGENT_PROFILE")
    if profile:
        warning(
            "WARP_AGENT_PROFILE is set, but the Oz Python SDK does not expose CLI profile support. Ignoring it."
        )

    # Opt runs into anyone-with-link viewer access so community members can
    # follow along via the session link. This relies on the server-side
    # `session_sharing.public_access` field added in APP-3762; the field is
    # typed once the Oz SDK is regenerated from the updated OpenAPI spec. In
    # the meantime the request body is serialized from this TypedDict
    # (total=False) so the extra key passes through at runtime.
    public_access = _resolve_session_sharing_public_access()
    if public_access is not None:
        cast(dict[str, Any], config)["session_sharing"] = {
            "public_access": public_access,
        }

    return config


def _normalize_skill_path(skill_name: str) -> str:
    """Normalize a short skill name into a repository-relative skill path."""
    if skill_name.endswith("SKILL.md"):
        return skill_name
    return f".agents/skills/{skill_name}/SKILL.md"


def _workflow_code_root() -> Path:
    """Return the checked-out workflow code root when available."""
    return workflow_code_root(__file__)


def _resolve_skill_location(skill_name: str) -> tuple[str, str, Path]:
    """Resolve a skill to the repo slug, relative path, and on-disk file location."""
    if ":" in skill_name:
        repo, skill_path = skill_name.split(":", 1)
        return repo, skill_path, Path(skill_path)

    skill_path = _normalize_skill_path(skill_name)
    consumer_repo_slug = repo_slug()
    consumer_repo_root = workspace()
    workflow_repo_root = _workflow_code_root()
    workflow_repo_slug = optional_env("WORKFLOW_CODE_REPOSITORY") or consumer_repo_slug

    candidates = [(consumer_repo_slug, consumer_repo_root)]
    if (workflow_repo_slug, workflow_repo_root) != (consumer_repo_slug, consumer_repo_root):
        candidates.append((workflow_repo_slug, workflow_repo_root))

    for candidate_repo_slug, candidate_root in candidates:
        candidate_path = candidate_root / skill_path
        if candidate_path.is_file():
            return candidate_repo_slug, skill_path, candidate_path

    checked_locations = ", ".join(
        str(candidate_root / skill_path) for _candidate_repo_slug, candidate_root in candidates
    )
    raise RuntimeError(
        f"Unable to resolve skill {skill_name!r}. Checked: {checked_locations}"
    )


def skill_file_path(skill_name: str) -> str:
    """Resolve a skill to the workspace-relative file path that the agent should read."""
    _repo_slug, skill_path, resolved_path = _resolve_skill_location(skill_name)
    if ":" in skill_name:
        return skill_path
    try:
        return resolved_path.relative_to(workspace()).as_posix()
    except ValueError:
        return resolved_path.as_posix()


def skill_spec(skill_name: str) -> str:
    """Resolve a skill name into a fully qualified spec, preferring consumer repo overrides."""
    resolved_repo_slug, skill_path, _resolved_path = _resolve_skill_location(skill_name)
    if ":" in skill_name:
        return skill_name
    return f"{resolved_repo_slug}:{skill_path}"


def dispatch_run(
    *,
    prompt: str,
    skill_name: str | None,
    title: str,
    config: AmbientAgentConfigParam,
) -> Any:
    """Dispatch an Oz agent run without waiting for it to finish.

    Returns the raw response object from ``client.agent.run`` (which
    carries at least a ``run_id`` attribute). Use this from contexts
    that cannot afford to block on the run completing — most notably
    the Vercel webhook handler, which must respond within GitHub's
    ~10s delivery window. The cron poller resumes the work later by
    calling ``client.agent.runs.retrieve(run_id)`` against the same
    run id.
    """
    client = build_oz_client()
    request: AgentRunParams = {
        "prompt": prompt,
        "title": title,
        "config": config,
        "team": True,
    }
    if skill_name:
        request["skill"] = skill_spec(skill_name)
    return client.agent.run(**request)


def run_agent(
    *,
    prompt: str,
    skill_name: str | None,
    title: str,
    config: AmbientAgentConfigParam,
    on_poll: Callable[[RunItem], None] | None = None,
    poll_interval_seconds: int = 30,
    timeout_seconds: int = 60 * 60,
) -> RunItem:
    """Run an Oz agent and poll until it reaches a terminal state.

    A blocking convenience wrapper around :func:`dispatch_run` plus a
    polling loop. GitHub Actions entrypoints use this; the Vercel
    control plane uses :func:`dispatch_run` directly and lets the cron
    poller drain runs asynchronously.
    """
    response = dispatch_run(
        prompt=prompt,
        skill_name=skill_name,
        title=title,
        config=config,
    )
    run_id = response.run_id
    client = build_oz_client()
    deadline = time.monotonic() + timeout_seconds
    last_state = None

    while True:
        run = client.agent.runs.retrieve(run_id)
        state = str(run.state)
        if state != last_state:
            notice(f"Oz run {run_id} state: {state}")
            last_state = state
        if on_poll:
            on_poll(run)
        if state in TERMINAL_STATES:
            if state != "SUCCEEDED":
                status = run.status_message
                message = status.message if status else None
                raise RuntimeError(message or f"Oz run {run_id} finished in state {state}")
            return run
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Oz run {run_id} did not finish before timeout")
        time.sleep(poll_interval_seconds)
