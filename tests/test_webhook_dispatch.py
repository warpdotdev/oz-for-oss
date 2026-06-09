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
from core.dispatch import DispatchRequest
from core.routing import (
    WORKFLOW_ACKNOWLEDGE_UNKNOWN_MENTION,
    WORKFLOW_ANNOUNCE_READY_ISSUE,
    WORKFLOW_PLAN_APPROVED,
    WORKFLOW_REVIEW_PR,
    WORKFLOW_TRIAGE_NEW_ISSUES,
)
from core.signatures import expected_signature
from core.state import InMemoryStateStore
from oz.attachments import text_attachment


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

    def test_dispatches_request_attachments(self) -> None:
        body, signature = _signed_envelope(self._payload())
        attachment = text_attachment(
            file_name="workflow-context.txt",
            text="attached workflow context",
        )

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
                attachments=(attachment,),
            )

        runner_calls: list[dict[str, Any]] = []

        def runner(**kwargs: Any) -> Any:
            runner_calls.append(kwargs)
            return SimpleNamespace(run_id="oz-run-attachments")

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="pull_request",
            delivery_id="delivery-attachments",
            secret=_SECRET,
            builder_registry={WORKFLOW_REVIEW_PR: builder},
            runner=runner,
            config_factory=lambda name, role: {"environment_id": "env", "name": name},
            store=InMemoryStateStore(),
        )

        self.assertEqual(response.status, 202)
        self.assertEqual(response.body["run_id"], "oz-run-attachments")
        self.assertEqual(runner_calls[0]["attachments"], (attachment,))

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

    def test_returns_202_dispatched_false_when_builder_skips(self) -> None:
        body, signature = _signed_envelope(self._payload())
        runner = MagicMock(side_effect=AssertionError("should not dispatch"))

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="pull_request",
            delivery_id="delivery-2b",
            secret=_SECRET,
            builder_registry={WORKFLOW_REVIEW_PR: lambda payload: None},
            runner=runner,
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
        )

        self.assertEqual(response.status, 202)
        self.assertFalse(response.body.get("dispatched", True))
        runner.assert_not_called()

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



class IssueTriageRouteConfigTest(unittest.TestCase):
    def _payload(self, *, login: str = "warp-dev-github-integration[bot]") -> dict[str, Any]:
        return {
            "action": "opened",
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 1234},
            "issue": {
                "number": 42,
                "labels": [],
                "assignees": [],
                "user": {"login": login, "type": "Bot"},
            },
        }

    def test_configured_bot_author_allowlist_routes_to_triage(self) -> None:
        body, signature = _signed_envelope(self._payload())
        store = InMemoryStateStore()

        def builder(payload: Mapping[str, Any]) -> DispatchRequest:
            return DispatchRequest(
                workflow=WORKFLOW_TRIAGE_NEW_ISSUES,
                repo="acme/widgets",
                installation_id=1234,
                config_name=WORKFLOW_TRIAGE_NEW_ISSUES,
                title="Triage issue #42",
                skill_name="triage-issue",
                prompt="prompt body",
                payload_subset={"issue_number": 42},
            )

        loader = MagicMock(return_value=["warp-dev-github-integration[bot]"])
        runner = MagicMock(return_value=SimpleNamespace(run_id="oz-run-triage"))

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="issues",
            delivery_id="delivery-triage-bot",
            secret=_SECRET,
            builder_registry={WORKFLOW_TRIAGE_NEW_ISSUES: builder},
            runner=runner,
            config_factory=lambda name, role: {"environment_id": "env", "name": name},
            store=store,
            triage_bot_author_allowlist_loader=loader,
        )

        self.assertEqual(response.status, 202)
        self.assertEqual(response.body["workflow"], WORKFLOW_TRIAGE_NEW_ISSUES)
        self.assertTrue(response.body["dispatched"])
        self.assertEqual(response.body["run_id"], "oz-run-triage")
        loader.assert_called_once()
        runner.assert_called_once()

    def test_unconfigured_bot_author_is_skipped_before_dispatch(self) -> None:
        body, signature = _signed_envelope(self._payload(login="dependabot[bot]"))
        runner = MagicMock(side_effect=AssertionError("should not dispatch"))
        loader = MagicMock(return_value=[])

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="issues",
            delivery_id="delivery-triage-dependabot",
            secret=_SECRET,
            builder_registry={},
            runner=runner,
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
            triage_bot_author_allowlist_loader=loader,
        )

        self.assertEqual(response.status, 202)
        self.assertIsNone(response.body["workflow"])
        self.assertEqual(response.body["reason"], "issue authored by automation user")
        loader.assert_called_once()
        runner.assert_not_called()



class SynchronousPlanApprovedPathTest(unittest.TestCase):
    def _payload(self) -> dict[str, Any]:
        return {
            "action": "labeled",
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 1234},
            "label": {"name": "plan-approved"},
            "pull_request": {
                "number": 121,
                "state": "open",
                "draft": False,
                "head": {"ref": "oz-agent/spec-issue-91"},
                "base": {"ref": "main"},
                "user": {"login": "alice", "type": "User"},
            },
            "sender": {"login": "alice"},
        }

    def test_synced_outcome_short_circuits_dispatch(self) -> None:
        body, signature = _signed_envelope(self._payload())

        sync_calls: list[Mapping[str, Any]] = []

        def sync_plan_approved(payload: Mapping[str, Any]) -> dict[str, Any]:
            sync_calls.append(payload)
            return {
                "action": "synced",
                "pr_number": 121,
                "linked_issue_number": 91,
                "comment_posted": True,
                "label_removed": True,
                "implementation_triggered": False,
            }

        builder_called = MagicMock()
        runner_called = MagicMock(side_effect=AssertionError("should not run"))

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="pull_request",
            delivery_id="delivery-pa-1",
            secret=_SECRET,
            builder_registry={WORKFLOW_PLAN_APPROVED: builder_called},
            runner=runner_called,
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
            sync_plan_approved=sync_plan_approved,
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(response.body["workflow"], WORKFLOW_PLAN_APPROVED)
        self.assertEqual(
            response.body["plan_approved"]["action"], "synced"
        )
        self.assertEqual(
            response.body["plan_approved"]["linked_issue_number"], 91
        )
        self.assertEqual(len(sync_calls), 1)
        builder_called.assert_not_called()

    def test_implementation_pending_falls_through_to_dispatch(self) -> None:
        body, signature = _signed_envelope(self._payload())

        builder = MagicMock()
        builder.return_value = DispatchRequest(
            workflow=WORKFLOW_PLAN_APPROVED,
            repo="acme/widgets",
            installation_id=1234,
            config_name="create-implementation-from-issue",
            title="Implement issue #91 (plan-approved)",
            skill_name="implement-specs",
            prompt="prompt body",
            payload_subset={"issue_number": 91, "linked_issue_number": 91},
        )
        runner = MagicMock(return_value=SimpleNamespace(run_id="oz-run-pa"))

        def sync_plan_approved(_payload: Mapping[str, Any]) -> dict[str, Any] | None:
            return None

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="pull_request",
            delivery_id="delivery-pa-2",
            secret=_SECRET,
            builder_registry={WORKFLOW_PLAN_APPROVED: builder},
            runner=runner,
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
            sync_plan_approved=sync_plan_approved,
        )
        self.assertEqual(response.status, 202)
        self.assertTrue(response.body["dispatched"])
        self.assertEqual(response.body["run_id"], "oz-run-pa")
        builder.assert_called_once()
        runner.assert_called_once()

    def test_500_when_sync_plan_approved_raises(self) -> None:
        body, signature = _signed_envelope(self._payload())

        def exploding_sync(_payload: Mapping[str, Any]) -> dict[str, Any] | None:
            raise RuntimeError("github outage")

        builder_called = MagicMock()
        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="pull_request",
            delivery_id="delivery-pa-3",
            secret=_SECRET,
            builder_registry={WORKFLOW_PLAN_APPROVED: builder_called},
            runner=lambda **_: SimpleNamespace(run_id="x"),
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
            sync_plan_approved=exploding_sync,
        )
        self.assertEqual(response.status, 500)
        self.assertIn("plan-approved path failed", response.body["error"])
        builder_called.assert_not_called()


class SynchronousAnnounceReadyIssuePathTest(unittest.TestCase):
    def _payload(self, *, label: str = "ready-to-implement") -> dict[str, Any]:
        return {
            "action": "labeled",
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 1234},
            "label": {"name": label},
            "issue": {
                "number": 42,
                "state": "open",
                "assignees": [{"login": "alice"}],
                "user": {"login": "alice", "type": "User"},
                "labels": [
                    {"name": "triaged"},
                    {"name": label},
                ],
            },
            "sender": {"login": "alice"},
        }

    def test_announce_outcome_short_circuits_dispatch(self) -> None:
        # The announce-ready-issue workflow is fully synchronous;
        # the webhook never falls through to a cloud-agent dispatch
        # so neither the builder nor the runner should be invoked.
        body, signature = _signed_envelope(self._payload())

        sync_calls: list[Mapping[str, Any]] = []

        def sync_announce(payload: Mapping[str, Any]) -> dict[str, Any]:
            sync_calls.append(payload)
            return {
                "action": "announced",
                "issue_number": 42,
                "label": "ready-to-implement",
            }

        builder_called = MagicMock()
        runner_called = MagicMock(side_effect=AssertionError("should not run"))

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="issues",
            delivery_id="delivery-ari-1",
            secret=_SECRET,
            builder_registry={},
            runner=runner_called,
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
            sync_announce_ready_issue=sync_announce,
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(
            response.body["workflow"], WORKFLOW_ANNOUNCE_READY_ISSUE
        )
        self.assertEqual(
            response.body["announce_ready_issue"]["action"], "announced"
        )
        self.assertEqual(
            response.body["announce_ready_issue"]["issue_number"], 42
        )
        self.assertEqual(len(sync_calls), 1)
        builder_called.assert_not_called()

    def test_returns_202_without_outcome_when_sync_helper_not_wired(self) -> None:
        # Pure-routing unit-test path: when the sync helper is not
        # wired in, the webhook still returns 202 with the routed
        # decision so the GitHub deliveries UI stays green. No
        # cloud-agent dispatch happens for this workflow regardless.
        body, signature = _signed_envelope(self._payload())

        runner_called = MagicMock(side_effect=AssertionError("should not run"))
        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="issues",
            delivery_id="delivery-ari-2",
            secret=_SECRET,
            builder_registry={},
            runner=runner_called,
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(
            response.body["workflow"], WORKFLOW_ANNOUNCE_READY_ISSUE
        )
        self.assertNotIn("announce_ready_issue", response.body)
        runner_called.assert_not_called()

    def test_500_when_sync_announce_raises(self) -> None:
        body, signature = _signed_envelope(self._payload())

        def exploding_sync(_payload: Mapping[str, Any]) -> dict[str, Any]:
            raise RuntimeError("github outage")

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="issues",
            delivery_id="delivery-ari-3",
            secret=_SECRET,
            builder_registry={},
            runner=lambda **_: SimpleNamespace(run_id="x"),
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
            sync_announce_ready_issue=exploding_sync,
        )
        self.assertEqual(response.status, 500)
        self.assertIn(
            "announce-ready-issue path failed", response.body["error"]
        )


class SynchronousAcknowledgeUnknownMentionPathTest(unittest.TestCase):
    def _payload(self, *, body: str = "@warp-bot please fix lint") -> dict[str, Any]:
        return {
            "action": "created",
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 1234},
            "issue": {"number": 42, "pull_request": {"url": "..."}},
            "comment": {
                "id": 7,
                "body": body,
                "user": {"login": "alice", "type": "User"},
            },
        }

    def test_acknowledge_outcome_short_circuits_dispatch(self) -> None:
        # The acknowledge-unknown-mention workflow is fully synchronous;
        # the webhook never falls through to a cloud-agent dispatch so
        # neither the builder nor the runner should be invoked.
        body, signature = _signed_envelope(self._payload())

        sync_calls: list[Mapping[str, Any]] = []

        def sync_acknowledge(payload: Mapping[str, Any]) -> dict[str, Any]:
            sync_calls.append(payload)
            return {
                "action": "acknowledged",
                "pr_number": 42,
                "mentioned_handle": "warp-bot",
            }

        runner_called = MagicMock(side_effect=AssertionError("should not run"))

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="issue_comment",
            delivery_id="delivery-aum-1",
            secret=_SECRET,
            builder_registry={},
            runner=runner_called,
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
            sync_acknowledge_unknown_mention=sync_acknowledge,
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(
            response.body["workflow"], WORKFLOW_ACKNOWLEDGE_UNKNOWN_MENTION
        )
        self.assertEqual(
            response.body["acknowledge_unknown_mention"]["action"],
            "acknowledged",
        )
        self.assertEqual(len(sync_calls), 1)
        runner_called.assert_not_called()

    def test_returns_202_without_outcome_when_sync_helper_not_wired(self) -> None:
        body, signature = _signed_envelope(self._payload())

        runner_called = MagicMock(side_effect=AssertionError("should not run"))
        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="issue_comment",
            delivery_id="delivery-aum-2",
            secret=_SECRET,
            builder_registry={},
            runner=runner_called,
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(
            response.body["workflow"], WORKFLOW_ACKNOWLEDGE_UNKNOWN_MENTION
        )
        self.assertNotIn("acknowledge_unknown_mention", response.body)
        runner_called.assert_not_called()

    def test_500_when_sync_acknowledge_raises(self) -> None:
        body, signature = _signed_envelope(self._payload())

        def exploding_sync(_payload: Mapping[str, Any]) -> dict[str, Any]:
            raise RuntimeError("github outage")

        response = process_webhook_request(
            body=body,
            signature_header=signature,
            event_header="issue_comment",
            delivery_id="delivery-aum-3",
            secret=_SECRET,
            builder_registry={},
            runner=lambda **_: SimpleNamespace(run_id="x"),
            config_factory=lambda name, role: {},
            store=InMemoryStateStore(),
            sync_acknowledge_unknown_mention=exploding_sync,
        )
        self.assertEqual(response.status, 500)
        self.assertIn(
            "acknowledge-unknown-mention path failed", response.body["error"]
        )


if __name__ == "__main__":
    unittest.main()
