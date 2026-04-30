from __future__ import annotations

import os
import unittest

from . import conftest  # noqa: F401

from oz.helpers import WorkflowProgressComment


class FakeComment:
    def __init__(self, repo: "FakeRepo", comment_id: int, body: str) -> None:
        self._repo = repo
        self.id = comment_id
        self.body = body
        self.user = {"login": "oz-agent[bot]"}

    def edit(self, body: str) -> None:
        self.body = body

    def delete(self) -> None:
        self._repo.comments = [
            comment
            for comment in self._repo.comments
            if comment.id != self.id
        ]


class FakeIssue:
    def __init__(self, repo: "FakeRepo") -> None:
        self._repo = repo

    def get_comments(self) -> list[FakeComment]:
        return list(self._repo.comments)

    def create_comment(self, body: str) -> FakeComment:
        comment = FakeComment(self._repo, self._repo.next_id, body)
        self._repo.next_id += 1
        self._repo.comments.append(comment)
        return comment

    def get_comment(self, comment_id: int) -> FakeComment:
        for comment in self._repo.comments:
            if comment.id == comment_id:
                return comment
        raise AssertionError(f"unknown comment id {comment_id}")


class FakeRepo:
    def __init__(self) -> None:
        self.comments: list[FakeComment] = []
        self.next_id = 1
        self.issue = FakeIssue(self)

    def get_issue(self, issue_number: int) -> FakeIssue:
        return self.issue


class WorkflowProgressCommentRunScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_github_run_id = os.environ.pop("GITHUB_RUN_ID", None)
        self._old_app_slug = os.environ.pop("GH_APP_SLUG", None)

    def tearDown(self) -> None:
        if self._old_github_run_id is not None:
            os.environ["GITHUB_RUN_ID"] = self._old_github_run_id
        if self._old_app_slug is not None:
            os.environ["GH_APP_SLUG"] = self._old_app_slug

    def test_new_run_creates_new_comment_without_github_run_id(self) -> None:
        repo = FakeRepo()
        first = WorkflowProgressComment(
            repo,  # type: ignore[arg-type]
            "acme",
            "widgets",
            42,
            workflow="review-pull-request",
            requester_login="alice",
            run_id="oz-run-1",
        )
        first.start("first review")

        second = WorkflowProgressComment(
            repo,  # type: ignore[arg-type]
            "acme",
            "widgets",
            42,
            workflow="review-pull-request",
            requester_login="alice",
            run_id="oz-run-2",
        )
        second.start("second review")

        self.assertEqual(len(repo.comments), 2)
        self.assertIn("first review", repo.comments[0].body)
        self.assertIn("oz-run-1", repo.comments[0].body)
        self.assertIn("second review", repo.comments[1].body)
        self.assertIn("oz-run-2", repo.comments[1].body)

    def test_same_run_updates_existing_comment_without_github_run_id(self) -> None:
        repo = FakeRepo()
        first = WorkflowProgressComment(
            repo,  # type: ignore[arg-type]
            "acme",
            "widgets",
            42,
            workflow="review-pull-request",
            requester_login="alice",
            run_id="oz-run-1",
        )
        first.start("review started")

        same_run = WorkflowProgressComment(
            repo,  # type: ignore[arg-type]
            "acme",
            "widgets",
            42,
            workflow="review-pull-request",
            requester_login="alice",
            run_id="oz-run-1",
        )
        same_run.complete("review completed")

        self.assertEqual(len(repo.comments), 1)
        self.assertIn("review started", repo.comments[0].body)
        self.assertIn("review completed", repo.comments[0].body)


if __name__ == "__main__":
    unittest.main()
