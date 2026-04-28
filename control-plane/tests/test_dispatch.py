"""Tests for ``control_plane.lib.dispatch``."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Mapping

from . import conftest  # noqa: F401

from lib.dispatch import (
    DispatchRequest,
    WORKFLOW_ROLES,
    dispatch_run,
    evaluate_route,
    role_for_workflow,
)
from lib.routing import RouteDecision
from lib.state import InMemoryStateStore, RUN_STATE_KEY_PREFIX


def _request(workflow: str = "review-pull-request", repo: str = "acme/widgets") -> DispatchRequest:
    return DispatchRequest(
        workflow=workflow,
        repo=repo,
        installation_id=12345,
        config_name="review-pull-request",
        title="PR review #1",
        skill_name="review-pr",
        prompt="prompt body",
        payload_subset={"pr_number": 1},
    )


def _config_factory(name: str, role: str) -> Mapping[str, Any]:
    return {"environment_id": f"env-{role}", "name": name}


def _runner_factory(run_id: str = "oz-run-1"):
    calls: list[dict[str, Any]] = []

    def runner(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(run_id=run_id)

    return runner, calls


class RoleForWorkflowTest(unittest.TestCase):
    def test_review_triage_role_for_review_workflow(self) -> None:
        self.assertEqual(role_for_workflow("review-pull-request"), "review-triage")

    def test_review_triage_role_for_triage_workflow(self) -> None:
        self.assertEqual(role_for_workflow("triage-new-issues"), "review-triage")

    def test_review_triage_role_for_respond_to_triaged_workflow(self) -> None:
        self.assertEqual(
            role_for_workflow("respond-to-triaged-issue-comment"), "review-triage"
        )

    def test_default_role_for_other_workflows(self) -> None:
        self.assertEqual(role_for_workflow("create-spec-from-issue"), "default")
        self.assertEqual(role_for_workflow("respond-to-pr-comment"), "default")

    def test_workflow_roles_constant_is_minimal(self) -> None:
        # Lock in the workflows that share the review-triage environment
        # so a future addition has to make a deliberate decision.
        self.assertEqual(
            set(WORKFLOW_ROLES.keys()),
            {
                "triage-new-issues",
                "respond-to-triaged-issue-comment",
                "review-pull-request",
            },
        )


class DispatchRunTest(unittest.TestCase):
    def test_persists_state_and_invokes_runner(self) -> None:
        runner, calls = _runner_factory()
        store = InMemoryStateStore()

        result = dispatch_run(
            request=_request(),
            runner=runner,
            config_factory=_config_factory,
            store=store,
        )

        self.assertEqual(len(calls), 1)
        invocation = calls[0]
        self.assertEqual(invocation["prompt"], "prompt body")
        self.assertEqual(invocation["title"], "PR review #1")
        self.assertEqual(invocation["skill"], "review-pr")
        self.assertTrue(invocation["team"])
        # Review workflows resolve to the review-triage role.
        self.assertEqual(
            invocation["config"],
            {"environment_id": "env-review-triage", "name": "review-pull-request"},
        )
        self.assertEqual(result.run_id, "oz-run-1")
        # The state record was persisted.
        keys = store.keys(RUN_STATE_KEY_PREFIX)
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].endswith("oz-run-1"))
        self.assertEqual(result.state.workflow, "review-pull-request")
        self.assertEqual(result.state.repo, "acme/widgets")
        self.assertEqual(result.state.installation_id, 12345)
        self.assertEqual(result.state.payload_subset, {"pr_number": 1})

    def test_uses_default_role_for_unregistered_workflow(self) -> None:
        runner, calls = _runner_factory()
        store = InMemoryStateStore()

        dispatch_run(
            request=_request(workflow="create-spec-from-issue"),
            runner=runner,
            config_factory=_config_factory,
            store=store,
        )

        invocation = calls[0]
        self.assertEqual(
            invocation["config"],
            {"environment_id": "env-default", "name": "review-pull-request"},
        )

    def test_raises_when_runner_returns_no_run_id(self) -> None:
        def runner(**_: Any) -> Any:
            return SimpleNamespace(run_id="")

        store = InMemoryStateStore()
        with self.assertRaises(RuntimeError):
            dispatch_run(
                request=_request(),
                runner=runner,
                config_factory=_config_factory,
                store=store,
            )
        # Nothing should have been persisted.
        self.assertEqual(store.keys(RUN_STATE_KEY_PREFIX), [])

    def test_validates_repo_slug(self) -> None:
        runner, _calls = _runner_factory()
        with self.assertRaises(ValueError):
            dispatch_run(
                request=_request(repo="not-a-slug"),
                runner=runner,
                config_factory=_config_factory,
                store=InMemoryStateStore(),
            )


class EvaluateRouteTest(unittest.TestCase):
    def test_returns_request_from_registered_builder(self) -> None:
        captured_payload: dict[str, Any] = {}

        def builder(payload: Mapping[str, Any]) -> DispatchRequest:
            captured_payload.update(payload)
            return _request()

        decision = RouteDecision("review-pull-request", "matched")
        request = evaluate_route(
            decision=decision,
            payload={"pr": {"number": 1}},
            builder_registry={"review-pull-request": builder},
        )
        self.assertIsNotNone(request)
        self.assertEqual(captured_payload, {"pr": {"number": 1}})

    def test_returns_none_when_no_builder_registered(self) -> None:
        decision = RouteDecision("create-spec-from-issue", "matched")
        request = evaluate_route(
            decision=decision,
            payload={},
            builder_registry={},
        )
        self.assertIsNone(request)

    def test_returns_none_for_skip_decision(self) -> None:
        decision = RouteDecision(None, "skipping")
        request = evaluate_route(
            decision=decision,
            payload={},
            builder_registry={"x": lambda payload: _request()},
        )
        self.assertIsNone(request)

    def test_raises_when_builder_returns_mismatched_workflow(self) -> None:
        def builder(_payload: Mapping[str, Any]) -> DispatchRequest:
            return _request(workflow="create-spec-from-issue")

        with self.assertRaises(RuntimeError):
            evaluate_route(
                decision=RouteDecision("review-pull-request", "matched"),
                payload={},
                builder_registry={"review-pull-request": builder},
            )


if __name__ == "__main__":
    unittest.main()
