"""Tests for ``control_plane.core.handlers``.

The handlers wire together:

- The artifact loader (``oz.artifacts.load_*_artifact``).
- The result applier (``workflows.<workflow>.apply_*_result``).
- The failure handler (``WorkflowProgressComment.report_error``).

The tests stub the ``workflows.*`` and ``oz.*`` modules so the
assertions stay focused on handler wiring (passing the right run state
into apply, calling the right artifact loader, etc).
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from . import conftest  # noqa: F401

from core.state import RunState


def _ensure_module(name: str) -> ModuleType:
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        sub = ".".join(parts[: i])
        if sub not in sys.modules:
            sys.modules[sub] = ModuleType(sub)
    module = ModuleType(name)
    if name == "oz":
        module.__path__ = [str(Path(__file__).resolve().parent.parent / "oz")]  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


class _HandlerTestBase(unittest.TestCase):
    """Mixin that owns the stub modules the handlers import lazily."""

    def setUp(self) -> None:
        super().setUp()
        self._module_keys = [
            "workflows",
            "workflows.review_pr",
            "workflows.respond_to_pr_comment",
            "workflows.verify_pr_comment",
            "workflows.triage_new_issues",
            "workflows.create_spec_from_issue",
            "workflows.create_implementation_from_issue",
            "oz",
            "oz.agent_workflow",
            "oz.artifacts",
            "oz.helpers",
            "oz.verification",
        ]
        self._original_modules = {
            key: sys.modules.get(key) for key in self._module_keys
        }
        # Always create fresh stubs that the handlers import lazily.
        workflows = _ensure_module("workflows")
        review = _ensure_module("workflows.review_pr")
        respond = _ensure_module("workflows.respond_to_pr_comment")
        verify = _ensure_module("workflows.verify_pr_comment")
        triage = _ensure_module("workflows.triage_new_issues")
        create_spec = _ensure_module("workflows.create_spec_from_issue")
        create_implementation = _ensure_module(
            "workflows.create_implementation_from_issue"
        )
        workflows.review_pr = review  # type: ignore[attr-defined]
        workflows.respond_to_pr_comment = respond  # type: ignore[attr-defined]
        workflows.verify_pr_comment = verify  # type: ignore[attr-defined]
        workflows.triage_new_issues = triage  # type: ignore[attr-defined]
        workflows.create_spec_from_issue = create_spec  # type: ignore[attr-defined]
        workflows.create_implementation_from_issue = create_implementation  # type: ignore[attr-defined]
        review.apply_review_result = MagicMock()  # type: ignore[attr-defined]
        respond.apply_pr_comment_result = MagicMock()  # type: ignore[attr-defined]
        verify.apply_verification_result = MagicMock()  # type: ignore[attr-defined]
        verify.VERIFICATION_REPORT_FILENAME = "verification_report.json"  # type: ignore[attr-defined]
        triage.apply_triage_result_for_dispatch = MagicMock()  # type: ignore[attr-defined]
        create_spec.apply_create_spec_result = MagicMock()  # type: ignore[attr-defined]
        create_implementation.apply_create_implementation_result = MagicMock()  # type: ignore[attr-defined]
        oz = _ensure_module("oz")
        artifacts = _ensure_module("oz.artifacts")
        helpers = _ensure_module("oz.helpers")
        verification = _ensure_module("oz.verification")
        oz.artifacts = artifacts  # type: ignore[attr-defined]
        oz.helpers = helpers  # type: ignore[attr-defined]
        oz.verification = verification  # type: ignore[attr-defined]
        artifacts.load_review_artifact = MagicMock(return_value={"summary": "ok"})  # type: ignore[attr-defined]
        artifacts.load_run_artifact = MagicMock(return_value={"overall_status": "passed"})  # type: ignore[attr-defined]
        artifacts.load_triage_artifact = MagicMock(return_value={"summary": "triage ok", "labels": []})  # type: ignore[attr-defined]
        # Track every reconstructed progress comment so individual
        # tests can assert ``complete`` / ``replace_body`` /
        # ``report_error`` were invoked on the right instance.
        self.progress_instances: list[MagicMock] = []

        def _progress_factory(*args: Any, **kwargs: Any) -> MagicMock:
            instance = MagicMock(
                comment_id=kwargs.get("comment_id") or 0,
                run_id=kwargs.get("run_id") or "",
                session_link=kwargs.get("session_link") or "",
                workflow=kwargs.get("workflow") or "",
                owner=args[1] if len(args) > 1 else "",
                repo=args[2] if len(args) > 2 else "",
                issue_number=args[3] if len(args) > 3 else 0,
            )
            self.progress_instances.append(instance)
            return instance

        helpers.WorkflowProgressComment = MagicMock(  # type: ignore[attr-defined]
            side_effect=_progress_factory
        )
        helpers.record_run_session_link = MagicMock()  # type: ignore[attr-defined]
        verification.list_downloadable_verification_artifacts = MagicMock(  # type: ignore[attr-defined]
            return_value=[]
        )

    def tearDown(self) -> None:
        for key, value in self._original_modules.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        super().tearDown()


def _state(workflow: str, *, payload_subset: dict[str, Any] | None = None) -> RunState:
    return RunState(
        run_id="run-1",
        workflow=workflow,
        repo="acme/widgets",
        installation_id=42,
        payload_subset=dict(
            payload_subset
            or {
                "owner": "acme",
                "repo": "widgets",
                "pr_number": 7,
                "requester": "alice",
            }
        ),
    )


def _factory(github_client: Any) -> Any:
    return lambda installation_id: github_client


class ReviewHandlersTest(_HandlerTestBase):
    def test_artifact_loader_calls_load_review_artifact(self) -> None:
        from core.handlers import build_review_handlers

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")
        handlers = build_review_handlers(_factory(github_client))
        result = handlers.artifact_loader("run-1")
        self.assertEqual(result, {"summary": "ok"})

    def test_result_applier_invokes_apply_review_result(self) -> None:
        from core.handlers import build_review_handlers

        github_client = MagicMock()
        repo_handle = MagicMock(name="repo")
        github_client.get_repo.return_value = repo_handle
        handlers = build_review_handlers(_factory(github_client))

        state = _state(
            "review-pull-request",
            payload_subset={
                "owner": "acme",
                "repo": "widgets",
                "pr_number": 7,
                "requester": "alice",
                "progress_comment_id": 8888,
            },
        )
        created_at = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
        terminal_run = SimpleNamespace(
            state="SUCCEEDED",
            created_at=created_at,
            artifacts=[SimpleNamespace(artifact_type="FILE")],
        )
        handlers.result_applier(
            state=state,
            result={"summary": "looks good"},
            run=terminal_run,
        )

        from workflows.review_pr import apply_review_result  # type: ignore[import-not-found]

        apply_review_result.assert_called_once()
        kwargs = apply_review_result.call_args.kwargs
        self.assertIs(kwargs["context"], state.payload_subset)
        self.assertEqual(kwargs["result"], {"summary": "looks good"})
        # The result_applier must hand a reconstructed progress
        # comment to ``apply_review_result`` so the final ``complete``
        # call lands on the same comment posted by the builder.
        self.assertIs(kwargs["progress"], self.progress_instances[-1])
        self.assertEqual(self.progress_instances[-1].comment_id, 8888)
        self.assertEqual(self.progress_instances[-1].run_id, "run-1")
        self.assertEqual(kwargs["run"].created_at, created_at)
        self.assertIs(kwargs["run"].artifacts, terminal_run.artifacts)

    def test_failure_handler_posts_workflow_error(self) -> None:
        from core.handlers import build_review_handlers

        github_client = MagicMock()
        repo_handle = MagicMock(name="repo")
        github_client.get_repo.return_value = repo_handle
        handlers = build_review_handlers(_factory(github_client))

        state = _state("review-pull-request")
        handlers.failure_handler(state=state, run=MagicMock(state="FAILED"))
        # The failure handler reconstructs the progress comment and
        # uses it to surface the error message in-place.
        self.assertEqual(len(self.progress_instances), 1)
        self.progress_instances[0].report_error.assert_called_once()

    def test_non_terminal_handler_records_session_link(self) -> None:
        from core.handlers import build_review_handlers

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")
        handlers = build_review_handlers(_factory(github_client))

        state = _state("review-pull-request")
        run = MagicMock(state="RUNNING", session_link="https://app.warp.dev/run/abc", run_id="oz-run-123")
        handlers.non_terminal_handler(state=state, run=run)
        helpers = sys.modules["oz.helpers"]
        helpers.record_run_session_link.assert_called_once_with(  # type: ignore[attr-defined]
            self.progress_instances[-1], run
        )


class RespondHandlersTest(_HandlerTestBase):
    def test_artifact_loader_returns_empty_dict(self) -> None:
        from core.handlers import build_respond_handlers

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")
        handlers = build_respond_handlers(_factory(github_client))

        # The respond-to-pr-comment loader is intentionally a no-op
        # because the apply step polls the optional artifacts itself.
        self.assertEqual(handlers.artifact_loader("run-1"), {})

    def test_result_applier_invokes_apply_pr_comment_result(self) -> None:
        from core.handlers import build_respond_handlers

        github_client = MagicMock()
        repo_handle = MagicMock(name="repo")
        github_client.get_repo.return_value = repo_handle
        handlers = build_respond_handlers(_factory(github_client))

        state = _state(
            "respond-to-pr-comment",
            payload_subset={
                "owner": "acme",
                "repo": "widgets",
                "pr_number": 7,
                "head_branch": "feature",
                "trigger_kind": "review",
                "review_reply_target_id": 999,
                "requester": "alice",
                "progress_comment_id": 6543,
            },
        )
        handlers.result_applier(state=state, result={})
        from workflows.respond_to_pr_comment import (  # type: ignore[import-not-found]
            apply_pr_comment_result,
        )

        apply_pr_comment_result.assert_called_once()
        kwargs = apply_pr_comment_result.call_args.kwargs
        self.assertIs(kwargs["context"], state.payload_subset)
        self.assertIs(kwargs["client"], github_client)
        self.assertIs(kwargs["progress"], self.progress_instances[-1])
        self.assertEqual(self.progress_instances[-1].comment_id, 6543)
        # The handler resolves the review-reply target so the
        # progress comment edits the inline review thread instead of
        # posting onto the PR conversation.
        repo_handle.get_pull.assert_called_once_with(7)


class VerifyHandlersTest(_HandlerTestBase):
    def test_artifact_loader_calls_load_run_artifact_with_report_filename(self) -> None:
        from core.handlers import build_verify_handlers

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")
        handlers = build_verify_handlers(_factory(github_client))

        handlers.artifact_loader("run-1")
        from oz.artifacts import (  # type: ignore[import-not-found]
            load_run_artifact,
        )

        load_run_artifact.assert_called_once_with(
            "run-1", filename="verification_report.json"
        )

    def test_result_applier_invokes_apply_verification_result(self) -> None:
        from core.handlers import build_verify_handlers

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")
        handlers = build_verify_handlers(_factory(github_client))

        state = _state("verify-pr-comment")
        terminal_run = SimpleNamespace(
            state="SUCCEEDED",
            artifacts=[SimpleNamespace(artifact_type="FILE")],
        )
        verification = sys.modules["oz.verification"]
        verification.list_downloadable_verification_artifacts.return_value = [  # type: ignore[attr-defined]
            {"title": "screenshot.png", "download_url": "https://example.test/a.png"}
        ]
        handlers.result_applier(
            state=state,
            result={"overall_status": "passed"},
            run=terminal_run,
        )
        from workflows.verify_pr_comment import (  # type: ignore[import-not-found]
            apply_verification_result,
        )

        apply_verification_result.assert_called_once()
        kwargs = apply_verification_result.call_args.kwargs
        self.assertEqual(kwargs["result"], {"overall_status": "passed"})
        self.assertIs(kwargs["progress"], self.progress_instances[-1])
        self.assertEqual(
            kwargs["artifacts"],
            [{"title": "screenshot.png", "download_url": "https://example.test/a.png"}],
        )
        artifacts_run = verification.list_downloadable_verification_artifacts.call_args.args[0]  # type: ignore[attr-defined]
        self.assertIs(artifacts_run.artifacts, terminal_run.artifacts)



class TriageHandlersTest(_HandlerTestBase):
    def _state(self) -> RunState:
        return _state(
            "triage-new-issues",
            payload_subset={
                "owner": "acme",
                "repo": "widgets",
                "issue_number": 91,
                "requester": "alice",
                "progress_comment_id": 7777,
                "configured_labels": {},
                "repo_label_names": [],
            },
        )

    def test_artifact_loader_calls_load_triage_artifact(self) -> None:
        from core.handlers import build_triage_handlers

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")
        handlers = build_triage_handlers(_factory(github_client))
        result = handlers.artifact_loader("run-1")
        self.assertEqual(result, {"summary": "triage ok", "labels": []})

    def test_result_applier_invokes_apply_triage_result_for_dispatch(self) -> None:
        from core.handlers import build_triage_handlers

        github_client = MagicMock()
        repo_handle = MagicMock(name="repo")
        github_client.get_repo.return_value = repo_handle
        handlers = build_triage_handlers(_factory(github_client))
        state = self._state()
        handlers.result_applier(state=state, result={"summary": "ok", "labels": []})
        from workflows.triage_new_issues import (  # type: ignore[import-not-found]
            apply_triage_result_for_dispatch,
        )

        apply_triage_result_for_dispatch.assert_called_once()
        kwargs = apply_triage_result_for_dispatch.call_args.kwargs
        self.assertIs(kwargs["context"], state.payload_subset)
        self.assertIs(kwargs["progress"], self.progress_instances[-1])
        self.assertEqual(self.progress_instances[-1].comment_id, 7777)
        self.assertEqual(self.progress_instances[-1].run_id, "run-1")

    def test_failure_handler_posts_workflow_error(self) -> None:
        from core.handlers import build_triage_handlers

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")
        handlers = build_triage_handlers(_factory(github_client))
        handlers.failure_handler(state=self._state(), run=MagicMock(state="FAILED"))
        self.assertEqual(len(self.progress_instances), 1)
        self.progress_instances[0].report_error.assert_called_once()

    def test_non_terminal_handler_records_session_link(self) -> None:
        from core.handlers import build_triage_handlers

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")
        handlers = build_triage_handlers(_factory(github_client))
        run = MagicMock(
            state="RUNNING",
            session_link="https://app.warp.dev/run/abc",
            run_id="oz-run-321",
        )
        handlers.non_terminal_handler(state=self._state(), run=run)
        helpers = sys.modules["oz.helpers"]
        helpers.record_run_session_link.assert_called_once_with(  # type: ignore[attr-defined]
            self.progress_instances[-1], run
        )


class HandlerRegistryTest(_HandlerTestBase):
    def test_registry_includes_all_pr_workflows(self) -> None:
        from core.handlers import build_handler_registry
        from core.routing import (
            WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
            WORKFLOW_CREATE_SPEC_FROM_ISSUE,
            WORKFLOW_PLAN_APPROVED,
            WORKFLOW_RESPOND_TO_PR_COMMENT,
            WORKFLOW_REVIEW_PR,
            WORKFLOW_TRIAGE_NEW_ISSUES,
            WORKFLOW_VERIFY_PR_COMMENT,
        )

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")
        registry = build_handler_registry(github_client_factory=_factory(github_client))
        self.assertEqual(
            set(registry.keys()),
            {
                WORKFLOW_REVIEW_PR,
                WORKFLOW_RESPOND_TO_PR_COMMENT,
                WORKFLOW_VERIFY_PR_COMMENT,
                WORKFLOW_TRIAGE_NEW_ISSUES,
                WORKFLOW_CREATE_SPEC_FROM_ISSUE,
                WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
                WORKFLOW_PLAN_APPROVED,
            },
        )

    def test_plan_approved_handlers_aliases_create_implementation(self) -> None:
        # ``plan-approved`` cloud runs land on the same
        # ``apply_create_implementation_result`` so the alias keeps
        # the cron poller's apply path uniform across both triggers.
        from core.handlers import build_plan_approved_handlers
        from core.state import RunState

        github_client = MagicMock()
        repo_handle = MagicMock(name="repo")
        github_client.get_repo.return_value = repo_handle
        handlers = build_plan_approved_handlers(_factory(github_client))

        state = RunState(
            run_id="run-pa-1",
            workflow="plan-approved",
            repo="acme/widgets",
            installation_id=42,
            payload_subset={
                "owner": "acme",
                "repo": "widgets",
                "issue_number": 91,
                "requester": "alice",
                "progress_comment_id": 5555,
            },
        )
        handlers.result_applier(state=state, result={"summary": "impl ok"})
        from workflows.create_implementation_from_issue import (  # type: ignore[import-not-found]
            apply_create_implementation_result,
        )

        apply_create_implementation_result.assert_called_once()
        kwargs = apply_create_implementation_result.call_args.kwargs
        self.assertIs(kwargs["context"], state.payload_subset)
        self.assertIs(kwargs["progress"], self.progress_instances[-1])


if __name__ == "__main__":
    unittest.main()
