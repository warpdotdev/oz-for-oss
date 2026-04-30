"""Tests for ``core.workflows.announce_ready_issue.apply_announce_ready_issue_sync``.

The webhook handler invokes ``apply_announce_ready_issue_sync``
synchronously on every ``issues.labeled`` delivery for
``ready-to-spec`` / ``ready-to-implement`` when ``oz-agent`` is not
already assigned. The helper posts a one-shot announcement comment on
the issue and never falls through to a cloud-agent dispatch path.

These tests stub ``oz.helpers`` so the assertions stay
focused on the sync helper's branching (announced vs. noop vs.
skipped).
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
        sub = ".".join(parts[:i])
        if sub not in sys.modules:
            sys.modules[sub] = ModuleType(sub)
    module = ModuleType(name)
    sys.modules[name] = module
    return module


def _comment(body: str) -> Any:
    return SimpleNamespace(body=body)


class _AnnounceReadyIssueTestBase(unittest.TestCase):
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

        # Stub the helpers used by ``apply_announce_ready_issue_sync``.
        helpers._workflow_metadata_prefix = MagicMock(  # type: ignore[attr-defined]
            return_value=(
                '<!-- oz-agent-metadata: {"type":"issue-status","workflow":"announce-ready-issue","issue":42'
            )
        )
        helpers.comment_metadata = MagicMock(  # type: ignore[attr-defined]
            return_value=(
                '<!-- oz-agent-metadata: {"type":"issue-status",'
                '"workflow":"announce-ready-issue","issue":42} -->'
            )
        )

        # Drop any cached import of announce_ready_issue so the test
        # picks up the helper stubs above.
        sys.modules.pop("workflows.announce_ready_issue", None)
        sys.modules.pop("announce_ready_issue", None)

    def tearDown(self) -> None:
        for key, value in self._original_modules.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        sys.modules.pop("workflows.announce_ready_issue", None)
        sys.modules.pop("announce_ready_issue", None)
        super().tearDown()


def _payload(
    *,
    label_name: str = "ready-to-implement",
    issue_number: int = 42,
    state: str = "open",
    assignees: list[str] | None = None,
    full_name: str = "acme/widgets",
) -> dict[str, Any]:
    return {
        "action": "labeled",
        "repository": {"full_name": full_name},
        "installation": {"id": 1234},
        "label": {"name": label_name},
        "issue": {
            "number": issue_number,
            "state": state,
            "assignees": [
                {"login": login} for login in (assignees or [])
            ],
            "user": {"login": "alice", "type": "User"},
        },
        "sender": {"login": "alice"},
    }


def _issue_handle(*, comments: list[str] | None = None) -> Any:
    handle = MagicMock(name="issue")
    handle.get_comments.return_value = [
        _comment(body) for body in (comments or [])
    ]
    return handle


def _repo_handle(*, issue: Any) -> Any:
    handle = MagicMock(name="repo_handle")
    handle.get_issue.return_value = issue
    return handle


class ApplyAnnounceReadyIssueSyncTest(_AnnounceReadyIssueTestBase):
    def test_announces_ready_to_implement_for_unassigned_issue(self) -> None:
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle()
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle, payload=_payload(label_name="ready-to-implement")
        )
        self.assertEqual(result["action"], "announced")
        self.assertEqual(result["issue_number"], 42)
        self.assertEqual(result["label"], "ready-to-implement")
        issue.create_comment.assert_called_once()
        body = issue.create_comment.call_args.args[0]
        self.assertIn("`ready-to-implement`", body)
        self.assertIn("@oz-agent", body)
        self.assertIn("You can also comment `@oz-agent`", body)
        self.assertNotIn("Maintainers can also comment", body)
        # Sanity-check the announcement encourages a code-change PR.
        self.assertIn("pull request", body.lower())

    def test_announces_ready_to_spec_for_unassigned_issue(self) -> None:
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle()
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle, payload=_payload(label_name="ready-to-spec")
        )
        self.assertEqual(result["action"], "announced")
        self.assertEqual(result["label"], "ready-to-spec")
        body = issue.create_comment.call_args.args[0]
        self.assertIn("`ready-to-spec`", body)
        self.assertIn("@oz-agent", body)
        self.assertIn("You can also comment `@oz-agent`", body)
        self.assertNotIn("Maintainers can also comment", body)
        # The spec announcement should reference the specs/ tree so
        # contributors know where the proposal belongs.
        self.assertIn("specs/", body)

    def test_idempotent_when_announcement_already_posted(self) -> None:
        # A prior announcement (matching the workflow metadata prefix)
        # should suppress the second post when the webhook redelivers.
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        prior = _comment(
            "Already announced.\n\n"
            '<!-- oz-agent-metadata: {"type":"issue-status",'
            '"workflow":"announce-ready-issue","issue":42} -->'
        )
        issue = _issue_handle()
        issue.get_comments.return_value = [prior]
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle, payload=_payload()
        )
        self.assertEqual(result["action"], "noop")
        self.assertEqual(result["issue_number"], 42)
        issue.create_comment.assert_not_called()

    def test_skips_when_oz_agent_is_assigned(self) -> None:
        # The sync helper re-validates the assignee gate so it stays
        # safe in isolation. With ``oz-agent`` assigned, the helper
        # short-circuits without posting (the spec/implementation
        # flow handles the assignment case via a different route).
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle()
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle,
            payload=_payload(assignees=["alice", "oz-agent"]),
        )
        self.assertEqual(result["action"], "skipped")
        self.assertIn("oz-agent", result["reason"])
        repo_handle.get_issue.assert_not_called()
        issue.create_comment.assert_not_called()

    def test_skips_unsupported_label(self) -> None:
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle()
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle, payload=_payload(label_name="bug")
        )
        self.assertEqual(result["action"], "skipped")
        self.assertIn("unsupported label", result["reason"])
        issue.create_comment.assert_not_called()

    def test_skips_closed_issue(self) -> None:
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle()
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle, payload=_payload(state="closed")
        )
        self.assertEqual(result["action"], "skipped")
        self.assertIn("not open", result["reason"])
        repo_handle.get_issue.assert_not_called()

    def test_skips_when_issue_payload_missing(self) -> None:
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        repo_handle = MagicMock(name="repo")
        result = apply_announce_ready_issue_sync(
            repo_handle,
            payload={
                "action": "labeled",
                "repository": {"full_name": "acme/widgets"},
                "label": {"name": "ready-to-implement"},
            },
        )
        self.assertEqual(result["action"], "skipped")
        self.assertIn("issue", result["reason"].lower())
        repo_handle.get_issue.assert_not_called()

    def test_skips_when_repository_full_name_missing(self) -> None:
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        repo_handle = MagicMock(name="repo")
        payload = _payload()
        payload["repository"] = {}
        result = apply_announce_ready_issue_sync(
            repo_handle, payload=payload
        )
        self.assertEqual(result["action"], "skipped")
        self.assertIn("full_name", result["reason"])

    def test_returns_skipped_when_create_comment_raises(self) -> None:
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle()
        issue.create_comment.side_effect = RuntimeError("github outage")
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle, payload=_payload()
        )
        self.assertEqual(result["action"], "skipped")
        self.assertIn("failed to post", result["reason"])


if __name__ == "__main__":
    unittest.main()
