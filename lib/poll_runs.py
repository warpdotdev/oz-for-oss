"""Drain in-flight cloud runs and apply terminal results to GitHub.

The Vercel cron task runs every minute (configured in ``vercel.json``)
and invokes :func:`drain_in_flight_runs`. For each in-flight record:

1. Retrieve the run via the Oz API.
2. If the run is still pending, increment the attempt counter and
   leave it in KV.
3. If the run reached a terminal SUCCEEDED state, fetch the workflow's
   named artifact and hand it to the workflow's :class:`ResultApplier`.
4. If the run reached a terminal failure state, hand control to the
   workflow's :class:`FailureHandler` so it can post an error comment
   on the originating issue/PR.

The poller never raises on per-run failures: each handler is wrapped in
``try/except`` so a single bad run state cannot stop the cron tick from
processing the rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .state import RunState, StateStore, delete_run_state, list_in_flight_runs, save_run_state

logger = logging.getLogger(__name__)

TERMINAL_STATES = {"SUCCEEDED", "FAILED", "ERROR", "CANCELLED"}
TERMINAL_FAILURE_STATES = {"FAILED", "ERROR", "CANCELLED"}


class RunRetriever(Protocol):
    """Read-only Oz client surface used by the poller."""

    def retrieve(self, run_id: str) -> Any: ...


class ArtifactLoader(Protocol):
    """Loads the workflow-specific result artifact for a completed run."""

    def __call__(self, run_id: str) -> dict[str, Any]: ...


class ResultApplier(Protocol):
    """Applies a successful run result back to GitHub."""

    def __call__(self, *, state: RunState, result: Mapping[str, Any]) -> None: ...


class FailureHandler(Protocol):
    """Posts a workflow failure message back to GitHub."""

    def __call__(self, *, state: RunState, run: Any) -> None: ...


class NonTerminalHandler(Protocol):
    """Updates the progress comment while a run is still pending.

    The cron poller invokes this hook on every poll where the Oz run
    has not yet reached a terminal state. It is the cron equivalent of
    the legacy ``on_poll`` callback the GHA path passes to
    :func:`run_agent` and is wired by :mod:`lib.handlers` to call
    :func:`oz_workflows.helpers.record_run_session_link`. Implementations
    must absorb their own exceptions so a transient GitHub API failure
    cannot abort the cron tick.
    """

    def __call__(self, *, state: RunState, run: Any) -> None: ...


@dataclass(frozen=True)
class WorkflowHandlers:
    """Per-workflow handlers used by :func:`drain_in_flight_runs`."""

    artifact_loader: ArtifactLoader
    result_applier: ResultApplier
    failure_handler: FailureHandler | None = None
    non_terminal_handler: NonTerminalHandler | None = None


@dataclass(frozen=True)
class DrainOutcome:
    """Per-run summary returned by :func:`drain_in_flight_runs`."""

    run_id: str
    workflow: str
    state: str
    applied: bool
    error: str = ""


def _coerce_state(run: Any) -> str:
    return str(getattr(run, "state", "") or "").strip().upper()


def _process_one(
    state: RunState,
    *,
    retriever: RunRetriever,
    handlers: Mapping[str, WorkflowHandlers],
    store: StateStore,
) -> DrainOutcome:
    handler = handlers.get(state.workflow)
    if handler is None:
        # Unrecognized workflow — drop the record so we don't keep
        # polling indefinitely. The webhook handler should not have
        # persisted this in the first place.
        delete_run_state(store, state.run_id)
        return DrainOutcome(
            run_id=state.run_id,
            workflow=state.workflow,
            state="UNKNOWN_WORKFLOW",
            applied=False,
            error=f"no handler registered for workflow {state.workflow!r}",
        )

    try:
        run = retriever.retrieve(state.run_id)
    except Exception as exc:
        logger.exception(
            "Failed to retrieve Oz run %s for workflow %s", state.run_id, state.workflow
        )
        # Bump the attempt counter and persist for the next cron tick.
        state.attempts += 1
        state.last_error = f"retrieve failed: {exc}"
        save_run_state(store, state)
        return DrainOutcome(
            run_id=state.run_id,
            workflow=state.workflow,
            state="RETRIEVE_FAILED",
            applied=False,
            error=str(exc),
        )

    current_state = _coerce_state(run)
    if current_state not in TERMINAL_STATES:
        state.attempts += 1
        # Drive the workflow's progress comment forward (e.g. with the
        # session-share link) before persisting the next-attempt
        # snapshot. Failures here are absorbed by the handler itself so
        # we do not let a transient GitHub API hiccup poison the rest
        # of the cron tick.
        if handler.non_terminal_handler is not None:
            try:
                handler.non_terminal_handler(state=state, run=run)
            except Exception:
                logger.exception(
                    "non_terminal_handler for run %s (%s) raised; ignoring",
                    state.run_id,
                    state.workflow,
                )
        save_run_state(store, state)
        return DrainOutcome(
            run_id=state.run_id,
            workflow=state.workflow,
            state=current_state or "PENDING",
            applied=False,
        )

    try:
        if current_state == "SUCCEEDED":
            result = handler.artifact_loader(state.run_id)
            handler.result_applier(state=state, result=result)
            applied = True
            error_message = ""
        else:
            if handler.failure_handler is not None:
                handler.failure_handler(state=state, run=run)
            applied = False
            error_message = current_state
    except Exception as exc:
        logger.exception(
            "Failed to apply Oz run %s for workflow %s",
            state.run_id,
            state.workflow,
        )
        # Apply failures keep the record so a future cron tick can
        # retry — but they bump the attempt counter so an operator can
        # see how many tries have been spent.
        state.attempts += 1
        state.last_error = f"apply failed: {exc}"
        save_run_state(store, state)
        return DrainOutcome(
            run_id=state.run_id,
            workflow=state.workflow,
            state=current_state,
            applied=False,
            error=str(exc),
        )

    delete_run_state(store, state.run_id)
    return DrainOutcome(
        run_id=state.run_id,
        workflow=state.workflow,
        state=current_state,
        applied=applied,
        error=error_message,
    )


def drain_in_flight_runs(
    *,
    store: StateStore,
    retriever: RunRetriever,
    handlers: Mapping[str, WorkflowHandlers],
    state_iterator: Callable[[StateStore], Any] = list_in_flight_runs,
) -> list[DrainOutcome]:
    """Process every in-flight run currently persisted in *store*.

    *state_iterator* is parameterized so tests can inject a
    deterministic iteration order without touching the underlying KV
    contract.
    """
    outcomes: list[DrainOutcome] = []
    for state in state_iterator(store):
        outcomes.append(
            _process_one(
                state,
                retriever=retriever,
                handlers=handlers,
                store=store,
            )
        )
    return outcomes


__all__ = [
    "ArtifactLoader",
    "DrainOutcome",
    "FailureHandler",
    "NonTerminalHandler",
    "ResultApplier",
    "RunRetriever",
    "TERMINAL_FAILURE_STATES",
    "TERMINAL_STATES",
    "WorkflowHandlers",
    "drain_in_flight_runs",
]
