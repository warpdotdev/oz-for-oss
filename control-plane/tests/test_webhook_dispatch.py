"""Tests for the dispatch path in ``api/webhook.py``.

The dispatch path runs after signature verification and routing. It
calls ``evaluate_route`` to turn a route decision into a
``DispatchRequest``, runs ``dispatch_run`` to start the cloud agent, and
returns 202 with the resulting run id.

The tests stub the builder registry, runner, config factory, and store
so we can exercise the wiring without GitHub or oz-agent SDKs.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import MagicMock

from . import conftest  # noqa: F401

from api.webhook import process_webhook_request
from lib.dispatch import DispatchRequest
from lib.routing import (
    WORKFLOW_ENFORCE_PR_ISSUE_STATE,
    WORKFLOW_REVIEW_PR,
)
from lib.signatures import expected_signature
from lib.state import InMemoryStateStore


_SECRET = "shared-test-secret"


def _signed_envelope(payload: dict[str, Any]) -> tuple[bytes, str]:
    body = json.dumps(payload).encode("utf-8")
    return body, expected_signature(_SECRET, body)


class DispatchPathTest(unittest.TestCase):
    def _payload(self) -> dict[str, Any]:
        return {
            "action": "opened",
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 1234},
            "pull_request": {
                "number": 42,
                "state": "open",
                "draft": False,
                "user": {"login": "carol", "type": "User"},
                "author_association": "MEMBER",
                "head": {"ref": "feature"},
                "base": {"ref": "main"},
            },
        }

    def test_dispatches_when_builder_returns_request(self) -> None:
        body, signature = _signed_envelope(self._payload())
        store = InMemoryStateStore()

        def builder(payload: Mapping[str, Any]) -> DispatchRequest:
            return DispatchRequest(
                workflow=WORKFLOW_REVIEW_PR,
                repo="acme/widgets",
                installation_id=1234,
                config_name=WORKFLOW_REVIEW_PR,
                title="PR review #42",
                skill_name="review-pr",
                prompt="prompt body",
                payload_subset={"pr_number": 42},
            )

        runner_calls: list[dict[str, Any]] = []

        def runner(**kwargs: Any) -> Any:
            runner_calls.append(kwargs)
            return SimpleNamespace(run_id="oz-run-1")

        config_factory_calls: list[tuple[str, str]] = []

        def config_factory(name: str, role: str) -> Mapping[str, Any]:
            config_factory_calls.append((name, role))
            return {"environment_id": "env", "name": name}

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="pull_request",
            delivery_id="delivery-1",
            secret=_SECRET,
            builder_registry={WORKFLOW_REVIEW_PR: builder},
            runner=runner,
            config_factory=config_factory,
            store=store,
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(response.body["workflow"], WORKFLOW_REVIEW_PR)
        self.assertTrue(response.body["dispatched"])
        self.assertEqual(response.body["run_id"], "oz-run-1")
        self.assertEqual(len(runner_calls), 1)

    def test_returns_202_dispatched_false_when_no_builder_registered(self) -> None:
        body, signature = _signed_envelope(self._payload())

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="pull_request",
            delivery_id="delivery-2",
            secret=_SECRET,
            builder_registry={},
            runner=lambda **_: SimpleNamespace(run_id="x"),
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
        )
        self.assertEqual(response.status, 202)
        self.assertFalse(response.body.get("dispatched", True))

    def test_returns_500_when_dispatch_run_raises(self) -> None:
        body, signature = _signed_envelope(self._payload())

        def builder(payload: Mapping[str, Any]) -> DispatchRequest:
            return DispatchRequest(
                workflow=WORKFLOW_REVIEW_PR,
                repo="acme/widgets",
                installation_id=1234,
                config_name=WORKFLOW_REVIEW_PR,
                title="PR review #42",
                skill_name=None,
                prompt="prompt",
                payload_subset={},
            )

        def exploding_runner(**_: Any) -> Any:
            raise RuntimeError("oz down")

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="pull_request",
            delivery_id="delivery-3",
            secret=_SECRET,
            builder_registry={WORKFLOW_REVIEW_PR: builder},
            runner=exploding_runner,
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
        )
        self.assertEqual(response.status, 500)
        self.assertIn("dispatch failed", response.body["error"])

    def test_returns_500_when_builder_raises(self) -> None:
        body, signature = _signed_envelope(self._payload())

        def exploding_builder(payload: Mapping[str, Any]) -> DispatchRequest:
            raise ValueError("payload missing")

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="pull_request",
            delivery_id="delivery-4",
            secret=_SECRET,
            builder_registry={WORKFLOW_REVIEW_PR: exploding_builder},
            runner=lambda **_: SimpleNamespace(run_id="x"),
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
        )
        self.assertEqual(response.status, 500)
        self.assertIn("builder failed", response.body["error"])


class SynchronousEnforcePathTest(unittest.TestCase):
    def _payload(self) -> dict[str, Any]:
        return {
            "action": "synchronize",
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 1234},
            "pull_request": {
                "number": 42,
                "state": "open",
                "draft": False,
                "user": {"login": "carol", "type": "User"},
                "author_association": "CONTRIBUTOR",
                "head": {"ref": "feature"},
                "base": {"ref": "main"},
            },
        }

    def test_synchronous_path_short_circuits_dispatch(self) -> None:
        body, signature = _signed_envelope(self._payload())

        sync_calls: list[Mapping[str, Any]] = []

        def sync_enforcer(payload: Mapping[str, Any]) -> dict[str, Any]:
            sync_calls.append(payload)
            return {
                "action": "allow",
                "reason": "associated-ready-issue",
                "allow_review": True,
            }

        builder_called = MagicMock()
        runner_called = MagicMock(side_effect=AssertionError("should not run"))

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="pull_request",
            delivery_id="delivery-1",
            secret=_SECRET,
            builder_registry={WORKFLOW_ENFORCE_PR_ISSUE_STATE: builder_called},
            runner=runner_called,
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
            sync_enforcer=sync_enforcer,
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(response.body["workflow"], WORKFLOW_ENFORCE_PR_ISSUE_STATE)
        self.assertEqual(
            response.body["enforce"],
            {
                "action": "allow",
                "reason": "associated-ready-issue",
                "allow_review": True,
            },
        )
        self.assertEqual(len(sync_calls), 1)
        builder_called.assert_not_called()

    def test_need_cloud_match_falls_through_to_dispatch(self) -> None:
        body, signature = _signed_envelope(self._payload())

        builder = MagicMock()
        builder.return_value = DispatchRequest(
            workflow=WORKFLOW_ENFORCE_PR_ISSUE_STATE,
            repo="acme/widgets",
            installation_id=1234,
            config_name=WORKFLOW_ENFORCE_PR_ISSUE_STATE,
            title="Associate PR #42 with ready issue",
            skill_name=None,
            prompt="prompt body",
            payload_subset={"pr_number": 42},
        )

        runner = MagicMock(return_value=SimpleNamespace(run_id="oz-run-2"))

        def sync_enforcer(_payload: Mapping[str, Any]) -> dict[str, Any] | None:
            return None  # signals need-cloud-match

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="pull_request",
            delivery_id="delivery-1",
            secret=_SECRET,
            builder_registry={WORKFLOW_ENFORCE_PR_ISSUE_STATE: builder},
            runner=runner,
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
            sync_enforcer=sync_enforcer,
        )
        self.assertEqual(response.status, 202)
        self.assertTrue(response.body["dispatched"])
        self.assertEqual(response.body["run_id"], "oz-run-2")
        builder.assert_called_once()
        runner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
