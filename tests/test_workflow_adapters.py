"""Tests for workflow dispatch and cron progress-comment wiring."""

from __future__ import annotations

import sys
import unittest
from types import ModuleType
from typing import Any

from . import conftest  # noqa: F401

from core.state import RunState


class _ProgressComment:
    instances: list["_ProgressComment"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.comment_id = 4242
        self.started_with: str | None = None
        self.recorded_oz_run_id: str | None = None
        _ProgressComment.instances.append(self)

    def start(self, line: str) -> None:
        self.started_with = line

    def record_oz_run_id(self, run_id: str) -> None:
        self.recorded_oz_run_id = run_id


class WorkflowProgressAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original_helpers = sys.modules.get("oz.helpers")
        helpers = ModuleType("oz.helpers")
        helpers.WorkflowProgressComment = _ProgressComment  # type: ignore[attr-defined]
        sys.modules["oz.helpers"] = helpers
        _ProgressComment.instances = []

    def tearDown(self) -> None:
        if self._original_helpers is None:
            sys.modules.pop("oz.helpers", None)
        else:
            sys.modules["oz.helpers"] = self._original_helpers

    def test_create_progress_comment_uses_dispatched_run_id(self) -> None:
        from oz.agent_workflow import ProgressCommentSpec, create_progress_comment

        repo_handle = object()
        progress = create_progress_comment(
            ProgressCommentSpec(
                repo_handle=repo_handle,
                owner="acme",
                repo="widgets",
                issue_number=12,
                workflow="review-pull-request",
                start_line="Starting review.",
                requester_login="alice",
            ),
            run_id="oz-run-123",
        )

        self.assertIs(progress, _ProgressComment.instances[0])
        self.assertEqual(progress.args[:4], (repo_handle, "acme", "widgets", 12))
        self.assertEqual(progress.kwargs["workflow"], "review-pull-request")
        self.assertEqual(progress.kwargs["requester_login"], "alice")
        self.assertEqual(progress.kwargs["run_id"], "oz-run-123")
        self.assertEqual(progress.started_with, "Starting review.")

    def test_reconstruct_progress_uses_persisted_run_id(self) -> None:
        from core.workflow_adapters import reconstruct_progress

        repo_handle = object()
        state = RunState(
            run_id="oz-run-123",
            workflow="respond-to-pr-comment",
            repo="acme/widgets",
            installation_id=42,
            payload_subset={
                "pr_number": 12,
                "requester": "alice",
                "progress_comment_id": 4242,
            },
        )

        progress = reconstruct_progress(
            repo_handle,
            state=state,
            workflow="respond-to-pr-comment",
        )

        self.assertIs(progress, _ProgressComment.instances[0])
        self.assertEqual(progress.args[:4], (repo_handle, "acme", "widgets", 12))
        self.assertEqual(progress.kwargs["workflow"], "respond-to-pr-comment")
        self.assertEqual(progress.kwargs["requester_login"], "alice")
        self.assertEqual(progress.kwargs["comment_id"], 4242)
        self.assertEqual(progress.kwargs["run_id"], "oz-run-123")


if __name__ == "__main__":
    unittest.main()
