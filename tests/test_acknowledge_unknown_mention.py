"""Tests for ``workflows.acknowledge_unknown_mention``.

The webhook handler invokes ``apply_acknowledge_unknown_mention_sync``
synchronously whenever a PR comment, review comment, or review body
mentions an agent-like-but-unrecognized handle (for example the
pre-rebrand ``@warp-bot`` typo). The helper posts a one-shot
acknowledgement comment pointing the user at ``@oz-agent`` and never
falls through to a cloud-agent dispatch.

These tests exercise the helper's branching (acknowledged vs. noop vs.
skipped) with a stubbed GitHub repo handle.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from . import conftest  # noqa: F401

from workflows.acknowledge_unknown_mention import (
    apply_acknowledge_unknown_mention_sync,
)


def _comment(body: str) -> Any:
    return SimpleNamespace(body=body)


def _issue_handle(*, comments: list[str] | None = None) -> Any:
    handle = MagicMock(name="issue")
    handle.get_comments.return_value = [_comment(body) for body in (comments or [])]
    return handle


def _repo_handle(*, issue: Any) -> Any:
    handle = MagicMock(name="repo_handle")
    handle.get_issue.return_value = issue
    return handle


def _pr_comment_payload(
    *,
    body: str = "@warp-bot please fix lint",
    pr_number: int = 42,
    comment_id: int = 7,
    full_name: str = "acme/widgets",
) -> dict[str, Any]:
    return {
        "action": "created",
        "repository": {"full_name": full_name},
        "installation": {"id": 1234},
        "issue": {"number": pr_number, "pull_request": {"url": "..."}},
        "comment": {
            "id": comment_id,
            "body": body,
            "user": {"login": "alice", "type": "User"},
        },
    }


def _review_payload(
    *,
    body: str = "@ozagent please update",
    pr_number: int = 42,
    review_id: int = 9,
    full_name: str = "acme/widgets",
) -> dict[str, Any]:
    return {
        "action": "submitted",
        "repository": {"full_name": full_name},
        "installation": {"id": 1234},
        "pull_request": {"number": pr_number},
        "review": {
            "id": review_id,
            "body": body,
            "user": {"login": "alice", "type": "User"},
        },
    }


class ApplyAcknowledgeUnknownMentionSyncTest(unittest.TestCase):
    def test_acknowledges_unrecognized_handle_on_pr_comment(self) -> None:
        issue = _issue_handle()
        repo_handle = _repo_handle(issue=issue)

        result = apply_acknowledge_unknown_mention_sync(
            repo_handle, payload=_pr_comment_payload()
        )

        self.assertEqual(result["action"], "acknowledged")
        self.assertEqual(result["pr_number"], 42)
        self.assertEqual(result["mentioned_handle"], "warp-bot")
        repo_handle.get_issue.assert_called_once_with(42)
        issue.create_comment.assert_called_once()
        body = issue.create_comment.call_args.args[0]
        self.assertIn("@warp-bot", body)
        self.assertIn("@oz-agent", body)
        # The idempotency marker is embedded so retries can dedupe.
        self.assertIn('"workflow":"acknowledge-unknown-mention"', body)
        self.assertIn('"trigger_comment":7', body)

    def test_acknowledges_unrecognized_handle_in_review_body(self) -> None:
        issue = _issue_handle()
        repo_handle = _repo_handle(issue=issue)

        result = apply_acknowledge_unknown_mention_sync(
            repo_handle, payload=_review_payload()
        )

        self.assertEqual(result["action"], "acknowledged")
        self.assertEqual(result["mentioned_handle"], "ozagent")
        body = issue.create_comment.call_args.args[0]
        self.assertIn('"trigger_comment":9', body)

    def test_idempotent_when_acknowledgement_already_posted(self) -> None:
        # A prior acknowledgement carrying the same trigger-comment marker
        # suppresses a duplicate post when the webhook redelivers.
        prior = (
            "Already acknowledged.\n\n"
            '<!-- oz-agent-metadata: {"type":"issue-status",'
            '"workflow":"acknowledge-unknown-mention","issue":42,'
            '"trigger_comment":7} -->'
        )
        issue = _issue_handle(comments=[prior])
        repo_handle = _repo_handle(issue=issue)

        result = apply_acknowledge_unknown_mention_sync(
            repo_handle, payload=_pr_comment_payload()
        )

        self.assertEqual(result["action"], "noop")
        self.assertEqual(result["pr_number"], 42)
        issue.create_comment.assert_not_called()

    def test_distinct_comment_still_acknowledged(self) -> None:
        # A prior acknowledgement for a *different* triggering comment must
        # not suppress acknowledging a new one on the same PR.
        prior = (
            "Already acknowledged a different comment.\n\n"
            '<!-- oz-agent-metadata: {"type":"issue-status",'
            '"workflow":"acknowledge-unknown-mention","issue":42,'
            '"trigger_comment":1} -->'
        )
        issue = _issue_handle(comments=[prior])
        repo_handle = _repo_handle(issue=issue)

        result = apply_acknowledge_unknown_mention_sync(
            repo_handle, payload=_pr_comment_payload(comment_id=7)
        )

        self.assertEqual(result["action"], "acknowledged")
        issue.create_comment.assert_called_once()

    def test_skips_when_no_unrecognized_mention(self) -> None:
        issue = _issue_handle()
        repo_handle = _repo_handle(issue=issue)

        result = apply_acknowledge_unknown_mention_sync(
            repo_handle, payload=_pr_comment_payload(body="thanks, looks good")
        )

        self.assertEqual(result["action"], "skipped")
        issue.create_comment.assert_not_called()

    def test_skips_when_repository_full_name_missing(self) -> None:
        issue = _issue_handle()
        repo_handle = _repo_handle(issue=issue)
        payload = _pr_comment_payload()
        payload["repository"] = {"full_name": ""}

        result = apply_acknowledge_unknown_mention_sync(
            repo_handle, payload=payload
        )

        self.assertEqual(result["action"], "skipped")
        self.assertIn("repository.full_name", result["reason"])
        issue.create_comment.assert_not_called()

    def test_skips_when_pr_number_missing(self) -> None:
        repo_handle = _repo_handle(issue=_issue_handle())
        payload = _pr_comment_payload()
        payload["issue"] = {"pull_request": {"url": "..."}}

        result = apply_acknowledge_unknown_mention_sync(
            repo_handle, payload=payload
        )

        self.assertEqual(result["action"], "skipped")
        self.assertIn("PR/issue number", result["reason"])

    def test_skips_when_comment_post_fails(self) -> None:
        issue = _issue_handle()
        issue.create_comment.side_effect = RuntimeError("github outage")
        repo_handle = _repo_handle(issue=issue)

        result = apply_acknowledge_unknown_mention_sync(
            repo_handle, payload=_pr_comment_payload()
        )

        self.assertEqual(result["action"], "skipped")
        self.assertIn("failed to post", result["reason"])


if __name__ == "__main__":
    unittest.main()
