"""Dispatch a cloud agent run for a routed webhook event.

The dispatcher takes a :class:`~control_plane.lib.routing.RouteDecision`
plus the webhook payload, builds the agent prompt + config, calls the
Oz API to start the run, and persists in-flight state for the cron
poller to drain.

This module intentionally keeps prompt construction abstract:
``PromptBuilder`` is a callable contract so the webhook handler can
plug in workflow-specific prompt builders without coupling the
dispatcher to GitHub/PR/Issue specifics. The default builders live
alongside the existing GitHub Actions entrypoints in
``.github/scripts/`` and are re-imported by the Vercel runtime when the
control plane is the active webhook target.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .routing import RouteDecision
from .state import RunState, StateStore, save_run_state


# Workflow → role string accepted by ``oz_workflows.oz_client.build_agent_config``.
# Triage and review runs use the dedicated ``review-triage`` environment when
# the operator provides ``WARP_REVIEW_TRIAGE_ENVIRONMENT_ID``; the rest fall
# back to the default environment.
_REVIEW_TRIAGE_ROLE = "review-triage"
_DEFAULT_ROLE = "default"

WORKFLOW_ROLES: Mapping[str, str] = {
    "triage-new-issues": _REVIEW_TRIAGE_ROLE,
    "respond-to-triaged-issue-comment": _REVIEW_TRIAGE_ROLE,
    "review-pull-request": _REVIEW_TRIAGE_ROLE,
}

# Default workflow-code repo for skill resolution. Each cloud agent run
# tells the Oz API where to fetch its core skill from via a fully
# qualified ``<owner>/<repo>:<path>`` spec. The control plane lives in
# ``warpdotdev/oz-for-oss`` so its bundled skills (``review-pr``,
# ``implement-issue``, ``verify-pr``, ``triage-issue``, etc.) are
# resolvable against that repo by default. Forks can override the
# default by setting ``WORKFLOW_CODE_REPOSITORY=owner/repo`` in the
# Vercel environment so their fork's bundled skills are used instead.
# Repo-local override skills (e.g. ``review-pr-local``) live in the
# consuming repo and are referenced inside the prompt body, not via
# this skill spec.
_DEFAULT_WORKFLOW_CODE_REPOSITORY = "warpdotdev/oz-for-oss"


def _resolve_workflow_code_repo() -> str:
    """Return the configured workflow-code repo slug (defaults to oz-for-oss)."""
    raw = os.environ.get("WORKFLOW_CODE_REPOSITORY", "").strip()
    if raw and "/" in raw:
        return raw
    return _DEFAULT_WORKFLOW_CODE_REPOSITORY


def cloud_skill_spec(skill_name: str, *, workflow_repo: str | None = None) -> str:
    """Format *skill_name* into the ``<repo>:<path>`` spec the Oz API requires.

    Pass-through when *skill_name* already contains a ``:`` separator.
    Otherwise: normalize the bare name into
    ``.agents/skills/<name>/SKILL.md`` and prepend the workflow-code
    repo (``WORKFLOW_CODE_REPOSITORY`` env override or
    ``warpdotdev/oz-for-oss`` by default).

    The Oz API rejects bare skill names with
    ``invalid skill_spec format: missing ':' separator``; this helper
    exists so the dispatcher can produce valid specs from inside the
    Vercel runtime, which has no filesystem access to the skill files
    that the legacy ``oz_workflows.oz_client.skill_spec`` checks against.
    """
    if not skill_name:
        return skill_name
    if ":" in skill_name:
        return skill_name
    repo = workflow_repo or _resolve_workflow_code_repo()
    if skill_name.endswith("SKILL.md"):
        skill_path = skill_name
    else:
        skill_path = f".agents/skills/{skill_name}/SKILL.md"
    return f"{repo}:{skill_path}"


def role_for_workflow(workflow: str) -> str:
    """Return the agent role string that should be used for *workflow*.

    Defaults to ``"default"`` for workflows without a registered role
    so future additions don't accidentally fall onto the review-triage
    environment without an explicit decision.
    """
    return WORKFLOW_ROLES.get(workflow, _DEFAULT_ROLE)


@dataclass(frozen=True)
class DispatchRequest:
    """Inputs the dispatcher needs to start a cloud run.

    The dispatcher is intentionally not coupled to the webhook payload
    shape; the webhook handler builds this dataclass out of the route
    decision plus the prompt-builder it picked.
    """

    workflow: str
    repo: str
    installation_id: int
    config_name: str
    title: str
    skill_name: str | None
    prompt: str
    payload_subset: dict[str, Any]


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of a dispatch call.

    ``run_id`` is the Oz run id returned by ``client.agent.run``.
    ``state`` is the saved record so the caller can include the
    in-flight summary in logs.
    """

    run_id: str
    state: RunState


class AgentRunner(Protocol):
    """Subset of the Oz SDK surface the dispatcher needs.

    The Oz Python SDK's ``client.agent.run(**kwargs)`` returns an
    object with at least a ``run_id`` attribute.
    """

    def __call__(
        self,
        *,
        prompt: str,
        title: str,
        config: Mapping[str, Any],
        skill: str | None,
        team: bool,
    ) -> Any: ...


PromptBuilder = Callable[[Mapping[str, Any]], DispatchRequest]
"""A function that turns a webhook payload into a :class:`DispatchRequest`.

The webhook handler maintains a registry of prompt builders keyed by
workflow name. A prompt builder may inspect the payload to fetch
additional GitHub state (e.g. PR diff context) before returning the
request.
"""


def dispatch_run(
    *,
    request: DispatchRequest,
    runner: AgentRunner,
    config_factory: Callable[[str, str], Mapping[str, Any]],
    store: StateStore,
) -> DispatchResult:
    """Start a cloud agent run for *request* and persist its state.

    *config_factory* takes ``(config_name, role)`` and returns the
    ``AmbientAgentConfigParam`` payload. Wiring it as a callable keeps
    the dispatcher independent of the SDK and lets tests inject a
    deterministic config.
    """
    if not request.workflow:
        raise ValueError("DispatchRequest.workflow must be a non-empty string")
    if not request.repo or "/" not in request.repo:
        raise ValueError("DispatchRequest.repo must be a 'owner/name' slug")
    role = role_for_workflow(request.workflow)
    config = dict(config_factory(request.config_name, role))
    skill = (
        cloud_skill_spec(request.skill_name)
        if request.skill_name
        else None
    )
    response = runner(
        prompt=request.prompt,
        title=request.title,
        config=config,
        skill=skill,
        team=True,
    )
    run_id = str(getattr(response, "run_id", "") or "")
    if not run_id:
        raise RuntimeError("Oz agent.run response did not include a run_id")
    state = RunState(
        run_id=run_id,
        workflow=request.workflow,
        repo=request.repo,
        installation_id=int(request.installation_id),
        payload_subset=dict(request.payload_subset),
    )
    save_run_state(store, state)
    return DispatchResult(run_id=run_id, state=state)


def evaluate_route(
    *,
    decision: RouteDecision,
    payload: Mapping[str, Any],
    builder_registry: Mapping[str, PromptBuilder],
) -> DispatchRequest | None:
    """Resolve a :class:`DispatchRequest` for *decision*, or ``None`` to skip.

    Returns ``None`` when the decision points at a workflow without a
    registered prompt builder. The webhook handler logs that case and
    drops the request without dispatching.
    """
    if decision.workflow is None:
        return None
    builder = builder_registry.get(decision.workflow)
    if builder is None:
        return None
    request = builder(payload)
    if request.workflow != decision.workflow:
        raise RuntimeError(
            f"prompt builder for {decision.workflow!r} returned mismatched "
            f"DispatchRequest.workflow={request.workflow!r}"
        )
    return request


__all__ = [
    "AgentRunner",
    "DispatchRequest",
    "DispatchResult",
    "PromptBuilder",
    "WORKFLOW_ROLES",
    "cloud_skill_spec",
    "dispatch_run",
    "evaluate_route",
    "role_for_workflow",
]
