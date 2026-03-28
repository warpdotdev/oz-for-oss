from __future__ import annotations

import unittest

from oz_workflows.helpers import append_comment_sections, build_comment_body, comment_metadata_prefix, WorkflowProgressComment


class CommentUpdateTest(unittest.TestCase):
    def test_appends_instead_of_replacing(self) -> None:
        metadata = "<!-- meta -->"
        existing = build_comment_body("@alice\n\nOz is working on this issue.\n\nSharing session at: https://example.test/session/123", metadata)
        updated = append_comment_sections(existing, metadata, ["I created a spec PR for this issue: https://example.test/pr/1"])
        self.assertIn("Sharing session at: https://example.test/session/123", updated)
        self.assertIn("I created a spec PR for this issue: https://example.test/pr/1", updated)
        self.assertTrue(updated.endswith(metadata))
    def test_progress_comment_keeps_history_in_single_comment(self) -> None:
        github = FakeGitHubClient()
        progress = WorkflowProgressComment(
            github,
            "acme",
            "widgets",
            42,
            workflow="create-spec-from-issue",
            requester_login="alice",
        )
        progress.start("Oz is starting work on product and tech specs for this issue.")
        progress.record_session_link("https://example.test/session/123")
        progress.complete("I created a spec PR for this issue: https://example.test/pr/1")

        self.assertEqual(len(github.comments), 1)
        body = github.comments[0]["body"]
        self.assertIn("@alice", body)
        self.assertIn("Oz is starting work on product and tech specs for this issue.", body)
        self.assertIn("Sharing session at: https://example.test/session/123", body)
        self.assertIn("I created a spec PR for this issue: https://example.test/pr/1", body)

    def test_separate_runs_create_separate_comments(self) -> None:
        """Two runs of the same workflow on the same issue must not share a comment."""
        github = FakeGitHubClient()
        run1 = WorkflowProgressComment(
            github, "acme", "widgets", 42,
            workflow="triage-new-issues",
            requester_login="alice",
        )
        run1.start("Run 1 starting.")
        run1.record_session_link("https://example.test/session/run1")

        run2 = WorkflowProgressComment(
            github, "acme", "widgets", 42,
            workflow="triage-new-issues",
            requester_login="alice",
        )
        run2.start("Run 2 starting.")
        run2.record_session_link("https://example.test/session/run2")

        self.assertEqual(len(github.comments), 2)
        body1 = github.comments[0]["body"]
        body2 = github.comments[1]["body"]
        self.assertIn("session/run1", body1)
        self.assertNotIn("session/run2", body1)
        self.assertIn("session/run2", body2)
        self.assertNotIn("session/run1", body2)

    def test_cleanup_deletes_all_workflow_comments(self) -> None:
        """cleanup() should remove comments from any run of the same workflow."""
        github = FakeGitHubClient()
        run1 = WorkflowProgressComment(
            github, "acme", "widgets", 7,
            workflow="review-pull-request",
            requester_login="bob",
        )
        run1.start("Run 1.")

        run2 = WorkflowProgressComment(
            github, "acme", "widgets", 7,
            workflow="review-pull-request",
            requester_login="bob",
        )
        run2.start("Run 2.")
        self.assertEqual(len(github.comments), 2)

        # A fresh instance (run3) should clean up both earlier comments.
        run3 = WorkflowProgressComment(
            github, "acme", "widgets", 7,
            workflow="review-pull-request",
            requester_login="bob",
        )
        run3.cleanup()
        prefix = comment_metadata_prefix("review-pull-request", 7)
        remaining = [c for c in github.comments if prefix in str(c.get("body", ""))]
        self.assertEqual(remaining, [])


class FakeGitHubClient:
    def __init__(self) -> None:
        self.comments: list[dict[str, object]] = []

    def list_issue_comments(self, owner: str, repo: str, issue_number: int) -> list[dict[str, object]]:
        return [dict(comment) for comment in self.comments]

    def create_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict[str, object]:
        comment = {"id": len(self.comments) + 1, "body": body}
        self.comments.append(comment)
        return dict(comment)

    def get_comment(self, owner: str, repo: str, comment_id: int) -> dict[str, object]:
        for comment in self.comments:
            if int(comment["id"]) == comment_id:
                return dict(comment)
        raise AssertionError(f"Missing comment {comment_id}")

    def update_comment(self, owner: str, repo: str, comment_id: int, body: str) -> dict[str, object]:
        for comment in self.comments:
            if int(comment["id"]) == comment_id:
                comment["body"] = body
                return dict(comment)
        raise AssertionError(f"Missing comment {comment_id}")

    def delete_comment(self, owner: str, repo: str, comment_id: int) -> None:
        self.comments = [c for c in self.comments if int(c["id"]) != comment_id]

    def list_issue_events(self, owner: str, repo: str, issue_number: int) -> list[dict[str, object]]:
        return []


if __name__ == "__main__":
    unittest.main()
