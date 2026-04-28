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
    response = runner(
        prompt=request.prompt,
        title=request.title,
        config=config,
        skill=request.skill_name,
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
    "dispatch_run",
    "evaluate_route",
    "role_for_workflow",
]
