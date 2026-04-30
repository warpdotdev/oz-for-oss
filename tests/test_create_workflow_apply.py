from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from . import conftest  # noqa: F401


class CreateImplementationApplyTest(unittest.TestCase):
    def _context(self) -> dict[str, object]:
        return {
            "owner": "acme",
            "repo": "widgets",
            "issue_number": 12,
            "target_branch": "oz-agent/implement-issue-12",
            "default_branch": "main",
            "issue_title": "Add retries",
            "issue_labels": [],
            "requester": "alice",
            "selected_spec_pr_number": 0,
            "selected_spec_pr_url": "",
            "has_existing_implementation_pr": False,
        }

    def test_rejects_sibling_branch_override(self) -> None:
        from core.workflows.create_implementation_from_issue import (
            apply_create_implementation_result,
        )

        progress = MagicMock()
        run_created_at = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
        run = SimpleNamespace(run_id="run-1", created_at=run_created_at)
        metadata = {
            "branch_name": "oz-agent/implement-issue-123",
            "pr_title": "feat: add retries",
            "pr_summary": "Closes #12\n\nSummary",
        }

        with patch(
            "core.workflows.create_implementation_from_issue.branch_updated_since",
            return_value=False,
        ) as branch_updated_since:
            apply_create_implementation_result(
                MagicMock(),
                context=self._context(),
                run=run,
                result=metadata,
                progress=progress,
            )

        branch_updated_since.assert_called_once()
        self.assertEqual(
            branch_updated_since.call_args.args[3],
            "oz-agent/implement-issue-12",
        )

    def test_accepts_delimiter_bounded_branch_override_and_uses_cushion(self) -> None:
        from core.workflows.create_implementation_from_issue import (
            apply_create_implementation_result,
        )

        progress = MagicMock()
        run_created_at = datetime(2026, 4, 30, 12, 0)
        run = SimpleNamespace(run_id="run-1", created_at=run_created_at)
        metadata = {
            "branch_name": "oz-agent/implement-issue-12-add-retries",
            "pr_title": "feat: add retries",
            "pr_summary": "Closes #12\n\nSummary",
        }

        with patch(
            "core.workflows.create_implementation_from_issue.branch_updated_since",
            return_value=False,
        ) as branch_updated_since:
            apply_create_implementation_result(
                MagicMock(),
                context=self._context(),
                run=run,
                result=metadata,
                progress=progress,
            )

        branch_updated_since.assert_called_once()
        self.assertEqual(
            branch_updated_since.call_args.args[3],
            "oz-agent/implement-issue-12-add-retries",
        )
        self.assertEqual(
            branch_updated_since.call_args.kwargs["created_after"],
            run_created_at.replace(tzinfo=timezone.utc) - timedelta(minutes=1),
        )


class CreateSpecApplyTest(unittest.TestCase):
    def test_branch_updated_since_uses_one_minute_cushion(self) -> None:
        from core.workflows.create_spec_from_issue import apply_create_spec_result

        progress = MagicMock()
        run_created_at = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
        run = SimpleNamespace(run_id="run-1", created_at=run_created_at)

        with patch(
            "core.workflows.create_spec_from_issue.branch_updated_since",
            return_value=False,
        ) as branch_updated_since:
            apply_create_spec_result(
                MagicMock(),
                context={
                    "owner": "acme",
                    "repo": "widgets",
                    "issue_number": 12,
                    "branch_name": "oz-agent/spec-issue-12",
                    "default_branch": "main",
                    "issue_title": "Add retries",
                    "requester": "alice",
                },
                run=run,
                result={
                    "pr_title": "spec: add retries",
                    "pr_summary": "Related issue: #12",
                },
                progress=progress,
            )

        branch_updated_since.assert_called_once()
        self.assertEqual(
            branch_updated_since.call_args.kwargs["created_after"],
            run_created_at - timedelta(minutes=1),
        )


if __name__ == "__main__":
    unittest.main()
