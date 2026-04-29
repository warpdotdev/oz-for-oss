"""Tests for ``control_plane.lib.poll_runs``."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Mapping

from . import conftest  # noqa: F401

from lib.poll_runs import (
    DrainOutcome,
    WorkflowHandlers,
    drain_in_flight_runs,
)
from lib.state import (
    InMemoryStateStore,
    RUN_STATE_KEY_PREFIX,
    RunState,
    save_run_state,
)


class _FakeRetriever:
    def __init__(self, runs: dict[str, Any]) -> None:
        self._runs = runs
        self.calls: list[str] = []

    def retrieve(self, run_id: str) -> Any:
        self.calls.append(run_id)
        if run_id not in self._runs:
            raise RuntimeError(f"unknown run {run_id!r}")
        return self._runs[run_id]


def _state(run_id: str = "run-1", workflow: str = "review-pull-request") -> RunState:
    return RunState(
        run_id=run_id,
        workflow=workflow,
        repo="acme/widgets",
        installation_id=42,
        payload_subset={"pr_number": 1},
    )


def _seed(store: InMemoryStateStore, *states: RunState) -> None:
    for state in states:
        save_run_state(store, state)


def _make_handlers(
    *,
    artifact_loader=None,
    result_applier=None,
    failure_handler=None,
    non_terminal_handler=None,
) -> Mapping[str, WorkflowHandlers]:
    return {
        "review-pull-request": WorkflowHandlers(
            artifact_loader=artifact_loader or (lambda run_id: {"summary": "ok"}),
            result_applier=result_applier or (lambda *, state, result: None),
            failure_handler=failure_handler,
            non_terminal_handler=non_terminal_handler,
        )
    }


class DrainInFlightRunsTest(unittest.TestCase):
    def test_skips_pending_runs(self) -> None:
        store = InMemoryStateStore()
        _seed(store, _state())
        retriever = _FakeRetriever({"run-1": SimpleNamespace(state="RUNNING")})

        outcomes = drain_in_flight_runs(
            store=store,
            retriever=retriever,
            handlers=_make_handlers(),
        )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].state, "RUNNING")
        self.assertFalse(outcomes[0].applied)
        # The state record should still be in KV with bumped attempts.
        self.assertEqual(len(store.keys(RUN_STATE_KEY_PREFIX)), 1)

    def test_pending_run_invokes_non_terminal_handler(self) -> None:
        """On a non-terminal poll, the handler must drive progress forward.

        This is the cron-side equivalent of the legacy ``on_poll``
        callback the GHA path passes to ``run_agent``: the handler is
        responsible for surfacing the session-share link on the
        progress comment as soon as Oz reports it.
        """
        store = InMemoryStateStore()
        _seed(store, _state())
        run = SimpleNamespace(
            state="RUNNING",
            session_link="https://app.warp.dev/run/abc",
            run_id="oz-run-123",
        )
        retriever = _FakeRetriever({"run-1": run})

        recorded: list[dict[str, Any]] = []

        def non_terminal(*, state: RunState, run: Any) -> None:
            recorded.append({"state": state, "run": run})

        outcomes = drain_in_flight_runs(
            store=store,
            retriever=retriever,
            handlers=_make_handlers(non_terminal_handler=non_terminal),
        )
        self.assertEqual(outcomes[0].state, "RUNNING")
        self.assertEqual(len(recorded), 1)
        self.assertIs(recorded[0]["run"], run)
        self.assertEqual(recorded[0]["state"].run_id, "run-1")
        # The state record stays in KV with bumped attempts.
        self.assertEqual(len(store.keys(RUN_STATE_KEY_PREFIX)), 1)

    def test_non_terminal_handler_failure_is_swallowed(self) -> None:
        """A bad ``non_terminal_handler`` must not stop the cron tick."""
        store = InMemoryStateStore()
        _seed(store, _state())
        retriever = _FakeRetriever({"run-1": SimpleNamespace(state="RUNNING")})

        def explode(*, state: RunState, run: Any) -> None:
            raise RuntimeError("github down")

        outcomes = drain_in_flight_runs(
            store=store,
            retriever=retriever,
            handlers=_make_handlers(non_terminal_handler=explode),
        )
        # The drain still surfaces the in-flight outcome and keeps
        # the record in KV for the next cron tick.
        self.assertEqual(outcomes[0].state, "RUNNING")
        self.assertEqual(len(store.keys(RUN_STATE_KEY_PREFIX)), 1)

    def test_succeeded_run_invokes_applier_and_drains_record(self) -> None:
        store = InMemoryStateStore()
        _seed(store, _state())
        retriever = _FakeRetriever({"run-1": SimpleNamespace(state="SUCCEEDED")})

        applied: list[dict[str, Any]] = []

        def applier(*, state: RunState, result: Mapping[str, Any]) -> None:
            applied.append({"state": state, "result": dict(result)})

        outcomes = drain_in_flight_runs(
            store=store,
            retriever=retriever,
            handlers=_make_handlers(
                artifact_loader=lambda run_id: {"summary": "looks good"},
                result_applier=applier,
            ),
        )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].state, "SUCCEEDED")
        self.assertTrue(outcomes[0].applied)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["result"], {"summary": "looks good"})
        self.assertEqual(applied[0]["state"].run_id, "run-1")
        # The KV record should be removed after a successful apply.
        self.assertEqual(store.keys(RUN_STATE_KEY_PREFIX), [])

    def test_failed_run_invokes_failure_handler_and_drains_record(self) -> None:
        store = InMemoryStateStore()
        _seed(store, _state())
        retriever = _FakeRetriever({"run-1": SimpleNamespace(state="FAILED")})

        failures: list[Any] = []

        def failure_handler(*, state: RunState, run: Any) -> None:
            failures.append({"state": state, "run": run})

        outcomes = drain_in_flight_runs(
            store=store,
            retriever=retriever,
            handlers=_make_handlers(failure_handler=failure_handler),
        )
        self.assertEqual(outcomes[0].state, "FAILED")
        self.assertFalse(outcomes[0].applied)
        self.assertEqual(outcomes[0].error, "FAILED")
        self.assertEqual(len(failures), 1)
        self.assertEqual(store.keys(RUN_STATE_KEY_PREFIX), [])

    def test_unknown_workflow_drops_record(self) -> None:
        store = InMemoryStateStore()
        _seed(store, _state(workflow="not-registered"))

        retriever = _FakeRetriever({})

        outcomes = drain_in_flight_runs(
            store=store,
            retriever=retriever,
            handlers={},
        )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].state, "UNKNOWN_WORKFLOW")
        self.assertEqual(store.keys(RUN_STATE_KEY_PREFIX), [])

    def test_retrieve_failure_keeps_record_for_retry(self) -> None:
        store = InMemoryStateStore()
        _seed(store, _state())

        class ExplodingRetriever:
            def retrieve(self, run_id: str) -> Any:
                raise RuntimeError("network down")

        outcomes = drain_in_flight_runs(
            store=store,
            retriever=ExplodingRetriever(),
            handlers=_make_handlers(),
        )
        self.assertEqual(outcomes[0].state, "RETRIEVE_FAILED")
        self.assertEqual(outcomes[0].error, "network down")
        # The record stays in KV so the next cron tick can retry.
        self.assertEqual(len(store.keys(RUN_STATE_KEY_PREFIX)), 1)

    def test_apply_failure_keeps_record_for_retry(self) -> None:
        store = InMemoryStateStore()
        _seed(store, _state())
        retriever = _FakeRetriever({"run-1": SimpleNamespace(state="SUCCEEDED")})

        def exploding_applier(*, state: RunState, result: Mapping[str, Any]) -> None:
            raise RuntimeError("github down")

        outcomes = drain_in_flight_runs(
            store=store,
            retriever=retriever,
            handlers=_make_handlers(result_applier=exploding_applier),
        )
        self.assertEqual(outcomes[0].state, "SUCCEEDED")
        self.assertFalse(outcomes[0].applied)
        self.assertEqual(outcomes[0].error, "github down")
        self.assertEqual(len(store.keys(RUN_STATE_KEY_PREFIX)), 1)

    def test_drain_processes_multiple_runs(self) -> None:
        store = InMemoryStateStore()
        _seed(store, _state(run_id="run-1"), _state(run_id="run-2"))
        retriever = _FakeRetriever(
            {
                "run-1": SimpleNamespace(state="SUCCEEDED"),
                "run-2": SimpleNamespace(state="RUNNING"),
            }
        )

        outcomes = drain_in_flight_runs(
            store=store,
            retriever=retriever,
            handlers=_make_handlers(),
        )
        states = {o.run_id: o.state for o in outcomes}
        self.assertEqual(states, {"run-1": "SUCCEEDED", "run-2": "RUNNING"})
        # Only the still-running record remains.
        keys = store.keys(RUN_STATE_KEY_PREFIX)
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].endswith("run-2"))


class DrainOutcomeTest(unittest.TestCase):
    def test_outcome_default_error_is_empty(self) -> None:
        outcome = DrainOutcome(run_id="run-1", workflow="x", state="SUCCEEDED", applied=True)
        self.assertEqual(outcome.error, "")


if __name__ == "__main__":
    unittest.main()
