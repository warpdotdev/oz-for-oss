"""Tests for ``core.workflows.announce_ready_issue.apply_announce_ready_issue_sync``.

The webhook handler invokes ``apply_announce_ready_issue_sync``
synchronously on every ``issues.labeled`` delivery for
``ready-to-spec`` / ``ready-to-implement`` when ``oz-agent`` is not
already assigned. The helper posts a one-shot announcement comment on
the issue and never falls through to a cloud-agent dispatch path.

These tests stub the ``oz.helpers`` module import surface, but wire
``get_login`` / ``is_automation_user`` to the *real* production
implementations (captured below before any stubbing happens) so the
assignment tests exercise actual bot-detection semantics rather than a
hand-copied approximation that could drift from the real helper.
"""

from __future__ import annotations

import sys
import unittest
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from github.GithubException import GithubException

# Captured at module-import time, before any test stubs ``oz.helpers``
# in ``sys.modules``, so these are the genuine production functions.
from oz.helpers import get_login as _real_get_login
from oz.helpers import is_automation_user as _real_is_automation_user

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


def _assignee(login: str) -> Any:
    return SimpleNamespace(login=login)


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
        self.helpers = helpers

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
        # Default to the real production implementations so most tests
        # exercise genuine bot-detection semantics. Tests that need a
        # specific predicate outcome override ``self.helpers.is_automation_user``
        # with a controlled mock before importing the handler.
        helpers.get_login = _real_get_login  # type: ignore[attr-defined]
        helpers.is_automation_user = _real_is_automation_user  # type: ignore[attr-defined]

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
    sender: dict[str, Any] | None = None,
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
        "sender": sender if sender is not None else {"login": "alice", "type": "User"},
    }


def _issue_handle(
    *, comments: list[str] | None = None, assignees: list[str] | None = None
) -> Any:
    """Build a mock issue handle with a live ``.assignees`` list.

    ``assignees`` models the *current* (freshly fetched) assignee
    state, which the handler now reads directly from the issue handle
    rather than trusting the webhook payload's snapshot.
    """
    handle = MagicMock(name="issue")
    handle.get_comments.return_value = [
        _comment(body) for body in (comments or [])
    ]
    handle.assignees = [_assignee(login) for login in (assignees or [])]
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

    def test_assigns_labeler_when_issue_is_unassigned(self) -> None:
        # APP-5520: a human labeler on an unassigned issue should be
        # assigned to it alongside the announcement comment.
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle()
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle,
            payload=_payload(sender={"login": "bob", "type": "User"}),
        )
        self.assertEqual(result["action"], "announced")
        self.assertTrue(result["assignee_added"])
        issue.add_to_assignees.assert_called_once_with("bob")
        issue.create_comment.assert_called_once()

    def test_skips_assignment_when_issue_already_has_an_assignee(self) -> None:
        # Decision 1: never add the labeler alongside an existing
        # assignee, even a human one unrelated to oz-agent.
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle(assignees=["carol"])
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle,
            payload=_payload(
                assignees=["carol"],
                sender={"login": "bob", "type": "User"},
            ),
        )
        self.assertEqual(result["action"], "announced")
        self.assertFalse(result["assignee_added"])
        issue.add_to_assignees.assert_not_called()
        issue.create_comment.assert_called_once()

    def test_assignment_decision_uses_live_assignees_not_stale_payload(self) -> None:
        # Race: a separate ``issues.assigned`` delivery can add a
        # human assignee between GitHub generating this payload and
        # this handler running. The payload snapshot still shows no
        # assignees, but the freshly fetched issue now has one, so
        # the labeler must not be added alongside it.
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle(assignees=["dave"])
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle,
            payload=_payload(
                assignees=[],
                sender={"login": "bob", "type": "User"},
            ),
        )
        repo_handle.get_issue.assert_called_once()
        self.assertEqual(result["action"], "announced")
        self.assertFalse(result["assignee_added"])
        issue.add_to_assignees.assert_not_called()
        issue.create_comment.assert_called_once()

    def test_aborts_announce_when_oz_agent_assigned_after_payload_snapshot(self) -> None:
        # Same race, but the concurrent ``issues.assigned`` delivery
        # assigned oz-agent itself: the spec/implementation flow now
        # owns this issue, so the synchronous announce path (and the
        # labeler assignment) must abort even though the stale payload
        # snapshot showed no assignees.
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle(assignees=["oz-agent"])
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle,
            payload=_payload(assignees=[], sender={"login": "bob", "type": "User"}),
        )
        repo_handle.get_issue.assert_called_once()
        self.assertEqual(result["action"], "skipped")
        self.assertIn("oz-agent", result["reason"])
        issue.add_to_assignees.assert_not_called()
        issue.create_comment.assert_not_called()

    def test_skips_assignment_when_labeler_is_a_bot(self) -> None:
        # Decision 2: skip the assignment when the actor who applied
        # the label is a bot, but the announcement still posts. Uses a
        # controlled mock for the predicate itself (real bot-shape
        # coverage lives in IsAutomationUserProductionHelperTest below)
        # so this test stays focused on the handler's branching.
        self.helpers.is_automation_user = MagicMock(return_value=True)
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle(assignees=[])
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle,
            payload=_payload(sender={"login": "some-app[bot]", "type": "Bot"}),
        )
        self.assertEqual(result["action"], "announced")
        self.assertFalse(result["assignee_added"])
        self.helpers.is_automation_user.assert_called_once()
        issue.add_to_assignees.assert_not_called()
        issue.create_comment.assert_called_once()

    def test_posts_comment_when_assignment_call_raises_github_exception(self) -> None:
        # Decision 3: a failed assignment call must never suppress the
        # announcement comment or fail the webhook. Only the specific
        # operational exceptions the handler catches (GithubException /
        # RequestException) should be swallowed this way.
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle(assignees=[])
        issue.add_to_assignees.side_effect = GithubException(
            422, {"message": "no repo access"}, None
        )
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle,
            payload=_payload(sender={"login": "bob", "type": "User"}),
        )
        self.assertEqual(result["action"], "announced")
        self.assertFalse(result["assignee_added"])
        issue.create_comment.assert_called_once()

    def test_unexpected_assignment_exception_propagates(self) -> None:
        # A programming defect (anything other than the operational
        # GithubException / RequestException surface) must not be
        # silently downgraded to a log line and a 202.
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle(assignees=[])
        issue.add_to_assignees.side_effect = TypeError("boom")
        repo_handle = _repo_handle(issue=issue)

        with self.assertRaises(TypeError):
            apply_announce_ready_issue_sync(
                repo_handle,
                payload=_payload(sender={"login": "bob", "type": "User"}),
            )

    def test_no_assignment_when_sender_is_absent(self) -> None:
        # An absent ``sender`` field must not crash the handler; the
        # announcement should still post.
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle(assignees=[])
        repo_handle = _repo_handle(issue=issue)
        payload = _payload()
        del payload["sender"]

        result = apply_announce_ready_issue_sync(repo_handle, payload=payload)
        self.assertEqual(result["action"], "announced")
        self.assertFalse(result["assignee_added"])
        issue.add_to_assignees.assert_not_called()
        issue.create_comment.assert_called_once()

    def test_no_assignment_when_sender_is_malformed(self) -> None:
        # A ``sender`` that isn't a mapping/user-shaped object (e.g. a
        # list, from a malformed or unexpected payload) must not crash
        # the handler either.
        from workflows.announce_ready_issue import apply_announce_ready_issue_sync

        issue = _issue_handle(assignees=[])
        repo_handle = _repo_handle(issue=issue)

        result = apply_announce_ready_issue_sync(
            repo_handle, payload=_payload(sender=["unexpected", "shape"])
        )
        self.assertEqual(result["action"], "announced")
        self.assertFalse(result["assignee_added"])
        issue.add_to_assignees.assert_not_called()
        issue.create_comment.assert_called_once()


class IsAutomationUserProductionHelperTest(unittest.TestCase):
    """Direct coverage of the real ``oz.helpers.is_automation_user``.

    Exercises the unmodified production helper (not the test module's
    stub) against the bot shapes GitHub actually sends, so a future
    change to the predicate's supported forms is caught here rather
    than only in a handler-level test with a controlled mock.
    """

    def test_recognizes_type_bot(self) -> None:
        self.assertTrue(
            _real_is_automation_user({"login": "some-app", "type": "Bot"})
        )

    def test_recognizes_bot_suffixed_login_regardless_of_type(self) -> None:
        # GitHub Apps always suffix their login with ``[bot]`` even in
        # payload shapes that report a different/missing ``type``.
        self.assertTrue(
            _real_is_automation_user({"login": "dependabot[bot]", "type": "User"})
        )

    def test_recognizes_object_shaped_bot_sender(self) -> None:
        self.assertTrue(
            _real_is_automation_user(
                SimpleNamespace(login="ci-bot[bot]", type="Bot")
            )
        )

    def test_human_sender_is_not_automation(self) -> None:
        self.assertFalse(
            _real_is_automation_user({"login": "alice", "type": "User"})
        )

    def test_none_sender_is_not_automation(self) -> None:
        self.assertFalse(_real_is_automation_user(None))


if __name__ == "__main__":
    unittest.main()
