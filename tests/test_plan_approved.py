"""Tests for ``core.workflows.plan_approved.apply_plan_approved_sync``.

The webhook handler invokes ``apply_plan_approved_sync`` synchronously
on every ``pull_request.labeled`` delivery for the ``plan-approved``
label. The helper short-circuits on PRs that aren't spec PRs, mutates
the payload to stash the resolved issue number, and decides whether
the cron-side cloud-agent dispatch path is needed.

The tests stub ``oz.helpers`` so the assertions stay focused
on the sync helper's branching (skip vs. synced vs. dispatch-needed).
"""

from __future__ import annotations

import sys
import unittest
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from . import conftest  # noqa: F401


def _ensure_module(name: str) -> ModuleType:
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        sub = ".".join(parts[: i])
        if sub not in sys.modules:
            sys.modules[sub] = ModuleType(sub)
    module = ModuleType(name)
    sys.modules[name] = module
    return module


def _label(name: str) -> Any:
    return SimpleNamespace(name=name)


def _assignee(login: str) -> Any:
    return SimpleNamespace(login=login)


def _comment(body: str) -> Any:
    return SimpleNamespace(body=body)


class _PlanApprovedTestBase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._module_keys = [
            "oz",
            "oz.helpers",
        ]
        self._original_modules = {
            key: sys.modules.get(key) for key in self._module_keys
        }
        oz = _ensure_module("oz")
        helpers = _ensure_module("oz.helpers")
        oz.helpers = helpers  # type: ignore[attr-defined]

        # Stub the helpers used by ``apply_plan_approved_sync``. The
        # real implementations are exercised by their own unit tests.
        helpers._workflow_metadata_prefix = MagicMock(  # type: ignore[attr-defined]
            return_value=(
                '<!-- oz-agent-metadata: {"type":"issue-status","workflow":"plan-approved","issue":91'
            )
        )
        helpers.comment_metadata = MagicMock(  # type: ignore[attr-defined]
            return_value=(
                '<!-- oz-agent-metadata: {"type":"issue-status",'
                '"workflow":"plan-approved","issue":91} -->'
            )
        )

        def _is_spec_only(changed_files: list[str]) -> bool:
            return bool(changed_files) and all(
                f.startswith("specs/") for f in changed_files
            )

        helpers.is_spec_only_pr = MagicMock(side_effect=_is_spec_only)  # type: ignore[attr-defined]
        helpers.resolve_issue_number_for_pr = MagicMock(return_value=91)  # type: ignore[attr-defined]

        # Drop any cached import of plan_approved so the test gets a
        # fresh module bound to the helpers stubs above.
        sys.modules.pop("workflows.plan_approved", None)
        sys.modules.pop("plan_approved", None)

    def tearDown(self) -> None:
        for key, value in self._original_modules.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        sys.modules.pop("workflows.plan_approved", None)
        sys.modules.pop("plan_approved", None)
        super().tearDown()


def _payload(
    *,
    state: str = "open",
    head_ref: str = "oz-agent/spec-issue-91",
    full_name: str = "acme/widgets",
    pr_number: int = 121,
) -> dict[str, Any]:
    return {
        "action": "labeled",
        "repository": {"full_name": full_name},
        "installation": {"id": 1234},
        "label": {"name": "plan-approved"},
        "pull_request": {
            "number": pr_number,
            "state": state,
            "head": {"ref": head_ref},
            "base": {"ref": "main"},
            "user": {"login": "alice", "type": "User"},
        },
        "sender": {"login": "alice"},
    }


def _repo_handle(
    *,
    pr_obj: Any,
    issue: Any,
) -> Any:
    handle = MagicMock(name="repo_handle")
    handle.get_pull.return_value = pr_obj
    handle.get_issue.return_value = issue
    return handle


def _pr_obj(
    *,
    head_ref: str = "oz-agent/spec-issue-91",
    filenames: list[str] | None = None,
) -> Any:
    pr = MagicMock(name="pr")
    pr.head = SimpleNamespace(ref=head_ref)
    pr.get_files.return_value = [
        SimpleNamespace(filename=name)
        for name in (filenames or ["specs/GH91/product.md", "specs/GH91/tech.md"])
    ]
    return pr


def _issue(
    *,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    comments: list[str] | None = None,
) -> Any:
    issue = MagicMock(name="issue")
    issue.labels = [_label(name) for name in (labels or [])]
    issue.assignees = [_assignee(login) for login in (assignees or [])]
    issue.get_comments.return_value = [_comment(body) for body in (comments or [])]
    return issue


class ApplyPlanApprovedSyncTest(_PlanApprovedTestBase):
    def test_skips_closed_pr(self) -> None:
        from workflows.plan_approved import apply_plan_approved_sync

        repo_handle = MagicMock(name="repo")
        result = apply_plan_approved_sync(
            repo_handle, payload=_payload(state="closed")
        )
        self.assertEqual(result, {"action": "skipped", "reason": "PR is not open"})
        repo_handle.get_pull.assert_not_called()

    def test_skips_non_spec_pr(self) -> None:
        # PR has neither a spec branch nor spec-only changed files.
        from workflows.plan_approved import apply_plan_approved_sync

        pr = _pr_obj(
            head_ref="feature/refactor",
            filenames=["src/main.py", "README.md"],
        )
        repo_handle = _repo_handle(pr_obj=pr, issue=_issue())
        payload = _payload(head_ref="feature/refactor")

        result = apply_plan_approved_sync(repo_handle, payload=payload)
        self.assertIsNotNone(result)
        assert result is not None  # narrow for mypy/static checks
        self.assertEqual(result["action"], "skipped")
        self.assertIn("not a spec PR", result["reason"])
        # No issue lookup happens once the spec-only gate fails.
        repo_handle.get_issue.assert_not_called()

    def test_skips_when_no_linked_issue(self) -> None:
        # Override the resolver BEFORE importing plan_approved so the
        # ``from oz.helpers import resolve_issue_number_for_pr``
        # binding inside the module picks up the no-issue stub. Then
        # re-import the module fresh so the override is honored.
        helpers = sys.modules["oz.helpers"]
        helpers.resolve_issue_number_for_pr = MagicMock(return_value=None)  # type: ignore[attr-defined]
        sys.modules.pop("workflows.plan_approved", None)
        from workflows.plan_approved import apply_plan_approved_sync

        # Use a non-spec-branch so the PR has to qualify via spec-only.
        pr = _pr_obj(head_ref="feature/spec-only", filenames=["specs/GH91/product.md"])
        repo_handle = _repo_handle(pr_obj=pr, issue=_issue())

        result = apply_plan_approved_sync(
            repo_handle, payload=_payload(head_ref="feature/spec-only")
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "skipped")
        self.assertIn("no linked issue", result["reason"])
        repo_handle.get_issue.assert_not_called()

    def test_synced_path_posts_comment_and_removes_label(self) -> None:
        # ``ready-to-spec`` is present and oz-agent is NOT assigned;
        # the helper should post the spec-approved comment, strip the
        # label, and return ``synced`` (no implementation dispatch).
        from workflows.plan_approved import apply_plan_approved_sync

        pr = _pr_obj()
        issue = _issue(labels=["ready-to-spec"], assignees=["alice"], comments=[])
        repo_handle = _repo_handle(pr_obj=pr, issue=issue)
        payload = _payload()

        result = apply_plan_approved_sync(repo_handle, payload=payload)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "synced")
        self.assertEqual(result["linked_issue_number"], 91)
        self.assertTrue(result["comment_posted"])
        self.assertTrue(result["label_removed"])
        self.assertFalse(result["implementation_triggered"])
        # Sync helper mutates the payload so the dispatch builder can
        # reuse the resolved number even when implementation IS needed.
        self.assertEqual(payload.get("linked_issue_number"), 91)
        issue.create_comment.assert_called_once()
        body = issue.create_comment.call_args.args[0]
        self.assertIn("PR #121", body)
        self.assertIn("https://github.com/acme/widgets/pull/121", body)
        issue.remove_from_labels.assert_called_once_with("ready-to-spec")

    def test_existing_comment_is_not_re_posted(self) -> None:
        # Idempotency: a prior plan-approved comment on the issue
        # should suppress the second post when the webhook redelivers.
        from workflows.plan_approved import apply_plan_approved_sync

        prior_comment = _comment(
            'A spec for this issue has been approved.\n\n'
            '<!-- oz-agent-metadata: {"type":"issue-status",'
            '"workflow":"plan-approved","issue":91} -->'
        )
        pr = _pr_obj()
        issue = _issue(
            labels=["ready-to-spec"],
            assignees=["alice"],
        )
        issue.get_comments.return_value = [prior_comment]
        repo_handle = _repo_handle(pr_obj=pr, issue=issue)

        result = apply_plan_approved_sync(repo_handle, payload=_payload())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "synced")
        self.assertFalse(result["comment_posted"])
        issue.create_comment.assert_not_called()
        # Label removal still runs idempotently regardless of comment dedupe.
        issue.remove_from_labels.assert_called_once_with("ready-to-spec")

    def test_implementation_pending_returns_none(self) -> None:
        # ``ready-to-implement`` + oz-agent assignee means implementation
        # is needed; the helper returns ``None`` so the webhook falls
        # through to the dispatch path. Comment + label removal still run.
        from workflows.plan_approved import apply_plan_approved_sync

        pr = _pr_obj()
        issue = _issue(
            labels=["ready-to-spec", "ready-to-implement"],
            assignees=["oz-agent"],
        )
        repo_handle = _repo_handle(pr_obj=pr, issue=issue)
        payload = _payload()

        result = apply_plan_approved_sync(repo_handle, payload=payload)
        self.assertIsNone(result)
        self.assertEqual(payload.get("linked_issue_number"), 91)
        issue.create_comment.assert_called_once()
        issue.remove_from_labels.assert_called_once_with("ready-to-spec")

    def test_spec_only_filenames_qualify_without_spec_branch(self) -> None:
        # PR on an unusual branch still qualifies if every changed
        # file lives under ``specs/``. This mirrors the spec-only
        # heuristic used by the sync helper.
        from workflows.plan_approved import apply_plan_approved_sync

        pr = _pr_obj(
            head_ref="human/edit-spec",
            filenames=["specs/GH91/product.md", "specs/GH91/tech.md"],
        )
        issue = _issue(labels=["ready-to-spec"], assignees=["alice"])
        repo_handle = _repo_handle(pr_obj=pr, issue=issue)

        result = apply_plan_approved_sync(
            repo_handle, payload=_payload(head_ref="human/edit-spec")
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "synced")


if __name__ == "__main__":
    unittest.main()
