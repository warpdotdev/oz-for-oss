"""Tests for deterministic PR issue-state enforcement."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from . import conftest  # noqa: F401

from workflows.review_pr import (  # type: ignore[import-not-found]
    check_pr_issue_state_for_review,
    enforce_pr_issue_state_for_review,
)


def _file(path: str) -> SimpleNamespace:
    return SimpleNamespace(filename=path)


def _issue(*labels: str, pull_request: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        labels=[SimpleNamespace(name=label) for label in labels],
        pull_request=pull_request,
    )


class _Comment:
    def __init__(self, body: str = "") -> None:
        self.body = body

    def edit(self, body: str) -> None:
        self.body = body


class _IssueWithComments:
    def __init__(self) -> None:
        self.comments: list[_Comment] = []

    def get_comments(self) -> list[_Comment]:
        return self.comments

    def create_comment(self, body: str) -> _Comment:
        comment = _Comment(body)
        self.comments.append(comment)
        return comment


class _Repo:
    requester = None

    def __init__(self, issues: dict[int, object]) -> None:
        self.issues = issues

    def get_issue(self, number: int) -> object:
        return self.issues[number]


class CheckPrIssueStateForReviewTest(unittest.TestCase):
    def _check(
        self,
        *,
        changed_files: list[str],
        issue_numbers: list[int],
        labels_by_issue: dict[int, list[str]],
        github_linked_issue_numbers: list[int] | None = None,
        explicit_issue_numbers: list[int] | None = None,
    ) -> dict:
        repo = _Repo(
            {
                number: _issue(*labels)
                for number, labels in labels_by_issue.items()
            }
        )
        association = {
            "same_repo_issue_numbers": issue_numbers,
            "github_linked_issues": [
                {
                    "owner": "acme",
                    "repo": "widgets",
                    "number": number,
                    "source": "closingIssuesReferences",
                }
                for number in (github_linked_issue_numbers or [])
            ],
            "deterministic_issue_numbers": [],
            "primary_issue_number": issue_numbers[0] if len(issue_numbers) == 1 else None,
            "ambiguous": len(issue_numbers) > 1,
        }
        with patch("workflows.review_pr.resolve_pr_association", return_value=association):
            return check_pr_issue_state_for_review(
                repo,  # type: ignore[arg-type]
                owner="acme",
                repo="widgets",
                pr=SimpleNamespace(number=1, body=""),
                changed_files=changed_files,
                explicit_issue_numbers=explicit_issue_numbers,
            )

    def test_spec_markdown_pr_requires_ready_to_spec(self) -> None:
        check = self._check(
            changed_files=["specs/GH10/product.md"],
            issue_numbers=[10],
            labels_by_issue={10: ["ready-to-spec"]},
            github_linked_issue_numbers=[10],
        )

        self.assertTrue(check["allowed"])
        self.assertTrue(check["spec_only"])
        self.assertEqual(check["required_label"], "ready-to-spec")
        self.assertEqual(check["ready_issue_numbers"], [10])

    def test_code_pr_requires_ready_to_implement(self) -> None:
        check = self._check(
            changed_files=["core/routing.py"],
            issue_numbers=[11],
            labels_by_issue={11: ["ready-to-implement"]},
            github_linked_issue_numbers=[11],
        )

        self.assertTrue(check["allowed"])
        self.assertFalse(check["spec_only"])
        self.assertEqual(check["required_label"], "ready-to-implement")

    def test_no_linked_issue_fails(self) -> None:
        check = self._check(
            changed_files=["core/routing.py"],
            issue_numbers=[],
            labels_by_issue={},
        )

        self.assertFalse(check["allowed"])
        self.assertEqual(check["issue_numbers"], [])

    def test_wrong_label_fails_with_issue_status(self) -> None:
        check = self._check(
            changed_files=["core/routing.py"],
            issue_numbers=[12],
            labels_by_issue={12: ["ready-to-spec"]},
            github_linked_issue_numbers=[12],
        )

        self.assertFalse(check["allowed"])
        self.assertEqual(check["issue_statuses"][0].readiness_labels, ["ready-to-spec"])

    def test_reserved_internal_label_overrides_ready_label(self) -> None:
        check = self._check(
            changed_files=["core/routing.py"],
            issue_numbers=[18],
            labels_by_issue={18: ["ready-to-implement", "warp:reserved-internal"]},
            github_linked_issue_numbers=[18],
        )

        self.assertFalse(check["allowed"])
        self.assertEqual(check["ready_issue_numbers"], [18])
        self.assertEqual(check["reserved_internal_issue_numbers"], [18])

    def test_multiple_issues_pass_when_any_issue_is_ready(self) -> None:
        check = self._check(
            changed_files=["core/routing.py"],
            issue_numbers=[13, 14],
            labels_by_issue={
                13: ["triaged"],
                14: ["ready-to-implement"],
            },
            github_linked_issue_numbers=[13, 14],
        )

        self.assertTrue(check["allowed"])
        self.assertEqual(check["ready_issue_numbers"], [14])

    def test_github_linked_issue_passes(self) -> None:
        check = self._check(
            changed_files=["core/routing.py"],
            issue_numbers=[15],
            labels_by_issue={15: ["ready-to-implement"]},
            github_linked_issue_numbers=[15],
        )

        self.assertTrue(check["allowed"])
        self.assertEqual(check["issue_numbers"], [15])

    def test_explicit_payload_issue_passes_without_pr_body_reference(self) -> None:
        check = self._check(
            changed_files=["core/routing.py"],
            issue_numbers=[16],
            labels_by_issue={16: ["ready-to-implement"]},
            explicit_issue_numbers=[16],
        )

        self.assertTrue(check["allowed"])
        self.assertEqual(check["issue_numbers"], [16])

    def test_deterministic_only_issue_does_not_satisfy_linked_issue_gate(self) -> None:
        check = self._check(
            changed_files=["core/routing.py"],
            issue_numbers=[17],
            labels_by_issue={17: ["ready-to-implement"]},
        )

        self.assertFalse(check["allowed"])
        self.assertEqual(check["issue_numbers"], [])


class EnforcePrIssueStateForReviewTest(unittest.TestCase):
    def test_blocked_pr_with_no_linked_issue_posts_actionable_comment(self) -> None:
        pr_issue = _IssueWithComments()
        repo = _Repo({7: pr_issue})
        repo.default_branch = "main"
        reviews_created: list[dict] = []

        def _create_review(body: str, event: str) -> None:
            reviews_created.append({"body": body, "event": event})

        pr = SimpleNamespace(
            number=7,
            body="",
            user=SimpleNamespace(login="external-user", type="User"),
            author_association="NONE",
            get_files=lambda: [_file("core/routing.py")],
            create_review=_create_review,
        )

        with patch(
            "workflows.review_pr.decode_repo_text_file",
            return_value="# Contributing",
        ):
            allowed = enforce_pr_issue_state_for_review(
                repo,  # type: ignore[arg-type]
                owner="acme",
                repo="widgets",
                pr=pr,
                requester="alice",
            )

        self.assertFalse(allowed)
        self.assertEqual(len(pr_issue.comments), 1)
        body = pr_issue.comments[0].body
        self.assertIn("@alice", body)
        self.assertIn(
            "Every PR must be linked to a same-repo issue before review can start.",
            body,
        )
        self.assertIn("Next step", body)
        self.assertIn("Closes #123", body)
        self.assertIn(
            "https://github.com/acme/widgets/blob/main/CONTRIBUTING.md",
            body,
        )
        # Verify REQUEST_CHANGES review was posted with the same body.
        self.assertEqual(len(reviews_created), 1)
        self.assertEqual(reviews_created[0]["event"], "REQUEST_CHANGES")
        self.assertIn(
            "Every PR must be linked to a same-repo issue before review can start.",
            reviews_created[0]["body"],
        )
        self.assertIn("_Powered by [Warp](https://www.warp.dev)_", reviews_created[0]["body"])

    def test_blocked_pr_with_linked_unready_issue_defers_to_maintainer(self) -> None:
        pr_issue = _IssueWithComments()
        linked_issue = _issue("triaged")
        repo = _Repo({9: pr_issue, 100: linked_issue})
        repo.default_branch = "develop"
        reviews_created: list[dict] = []

        def _create_review(body: str, event: str) -> None:
            reviews_created.append({"body": body, "event": event})

        pr = SimpleNamespace(
            number=9,
            body="",
            user=SimpleNamespace(login="external-user", type="User"),
            author_association="NONE",
            get_files=lambda: [_file("core/routing.py")],
            create_review=_create_review,
        )

        association = {
            "same_repo_issue_numbers": [100],
            "github_linked_issues": [
                {
                    "owner": "acme",
                    "repo": "widgets",
                    "number": 100,
                    "source": "closingIssuesReferences",
                }
            ],
            "deterministic_issue_numbers": [],
            "primary_issue_number": 100,
            "ambiguous": False,
        }
        with patch(
            "workflows.review_pr.resolve_pr_association",
            return_value=association,
        ), patch(
            "workflows.review_pr.decode_repo_text_file",
            return_value="# Contributing",
        ):
            allowed = enforce_pr_issue_state_for_review(
                repo,  # type: ignore[arg-type]
                owner="acme",
                repo="widgets",
                pr=pr,
                requester="alice",
            )

        self.assertFalse(allowed)
        self.assertEqual(len(pr_issue.comments), 1)
        body = pr_issue.comments[0].body
        self.assertIn("#100", body)
        self.assertIn("Only repository maintainers apply that label", body)
        self.assertIn("`ready-to-implement`", body)
        self.assertIn(
            "https://github.com/acme/widgets/blob/develop/CONTRIBUTING.md",
            body,
        )
        self.assertEqual(reviews_created[0]["event"], "REQUEST_CHANGES")

    def test_blocked_pr_with_reserved_internal_issue_rejects_contributor_pr(self) -> None:
        pr_issue = _IssueWithComments()
        linked_issue = _issue("ready-to-implement", "warp:reserved-internal")
        repo = _Repo({10: pr_issue, 101: linked_issue})
        repo.default_branch = "main"
        reviews_created: list[dict] = []

        def _create_review(body: str, event: str) -> None:
            reviews_created.append({"body": body, "event": event})

        pr = SimpleNamespace(
            number=10,
            body="",
            user=SimpleNamespace(login="external-user", type="User"),
            author_association="NONE",
            get_files=lambda: [_file("core/routing.py")],
            create_review=_create_review,
        )

        association = {
            "same_repo_issue_numbers": [101],
            "github_linked_issues": [
                {
                    "owner": "acme",
                    "repo": "widgets",
                    "number": 101,
                    "source": "closingIssuesReferences",
                }
            ],
            "deterministic_issue_numbers": [],
            "primary_issue_number": 101,
            "ambiguous": False,
        }
        with patch(
            "workflows.review_pr.resolve_pr_association",
            return_value=association,
        ), patch(
            "workflows.review_pr.decode_repo_text_file",
            return_value="# Contributing",
        ):
            allowed = enforce_pr_issue_state_for_review(
                repo,  # type: ignore[arg-type]
                owner="acme",
                repo="widgets",
                pr=pr,
                requester="alice",
            )

        self.assertFalse(allowed)
        self.assertEqual(len(pr_issue.comments), 1)
        body = pr_issue.comments[0].body
        self.assertIn("#101", body)
        self.assertIn("`warp:reserved-internal`", body)
        self.assertIn("reserving the work for internal implementation", body)
        self.assertIn("not accepting contributor PRs", body)
        self.assertIn(
            "https://github.com/acme/widgets/blob/main/CONTRIBUTING.md",
            body,
        )
        self.assertEqual(reviews_created[0]["event"], "REQUEST_CHANGES")
        self.assertIn("`warp:reserved-internal`", reviews_created[0]["body"])

    def test_blocked_pr_without_contributing_md_omits_link(self) -> None:
        pr_issue = _IssueWithComments()
        repo = _Repo({11: pr_issue})
        repo.default_branch = "main"
        reviews_created: list[dict] = []

        pr = SimpleNamespace(
            number=11,
            body="",
            user=SimpleNamespace(login="external-user", type="User"),
            author_association="NONE",
            get_files=lambda: [_file("core/routing.py")],
            create_review=lambda body, event: reviews_created.append(
                {"body": body, "event": event}
            ),
        )

        with patch(
            "workflows.review_pr.decode_repo_text_file",
            return_value=None,
        ):
            enforce_pr_issue_state_for_review(
                repo,  # type: ignore[arg-type]
                owner="acme",
                repo="widgets",
                pr=pr,
                requester="alice",
            )

        body = pr_issue.comments[0].body
        self.assertIn(
            "Every PR must be linked to a same-repo issue before review can start.",
            body,
        )
        self.assertNotIn("CONTRIBUTING.md", body)

    def test_org_member_pr_skips_enforcement(self) -> None:
        pr_issue = _IssueWithComments()
        repo = _Repo({8: pr_issue})

        pr = SimpleNamespace(
            number=8,
            body="",
            user=SimpleNamespace(login="org-member", type="User"),
            author_association="MEMBER",
            get_files=lambda: [_file("core/routing.py")],
            create_review=lambda body, event: None,
        )

        allowed = enforce_pr_issue_state_for_review(
            repo,  # type: ignore[arg-type]
            owner="acme",
            repo="widgets",
            pr=pr,
            requester="org-member",
        )

        self.assertTrue(allowed)
        self.assertEqual(len(pr_issue.comments), 0)


if __name__ == "__main__":
    unittest.main()
