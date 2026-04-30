"""Tests for deterministic single-reviewer selection in review_pr."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from . import conftest  # noqa: F401

from workflows.review_pr import (  # type: ignore[import-not-found]
    _deterministic_reviewer_from_stakeholders,
    _format_non_member_review_section,
    _format_review_completion_message,
    _parse_verdict,
    _resolve_recommended_reviewers,
    _stakeholder_pattern_matches,
    apply_review_result,
)


STAKEHOLDERS = [
    {"pattern": "*", "owners": ["fallback"]},
    {"pattern": "/docs/", "owners": ["docs-owner"]},
    {"pattern": "/docs/api/", "owners": ["api-owner"]},
    {"pattern": "/src/*.py", "owners": ["python-owner"]},
]


class StakeholderPatternMatchingTest(unittest.TestCase):
    def test_matches_root_anchored_directory_patterns(self) -> None:
        self.assertTrue(_stakeholder_pattern_matches("/docs/", "docs/readme.md"))
        self.assertFalse(_stakeholder_pattern_matches("/docs/", "src/docs/readme.md"))

    def test_matches_glob_patterns(self) -> None:
        self.assertTrue(_stakeholder_pattern_matches("/src/*.py", "src/app.py"))
        self.assertFalse(_stakeholder_pattern_matches("/src/*.py", "src/app.ts"))

    def test_matches_basename_patterns_anywhere(self) -> None:
        self.assertTrue(_stakeholder_pattern_matches("README.md", "docs/README.md"))
        self.assertFalse(_stakeholder_pattern_matches("README.md", "docs/README.txt"))


class DeterministicReviewerFallbackTest(unittest.TestCase):
    def test_uses_last_matching_stakeholder_rule_for_changed_path(self) -> None:
        reviewers = _deterministic_reviewer_from_stakeholders(
            STAKEHOLDERS,
            changed_paths=["docs/api/reference.md"],
            pr_author_login="contributor",
        )
        self.assertEqual(reviewers, ["api-owner"])

    def test_walks_changed_paths_in_order(self) -> None:
        reviewers = _deterministic_reviewer_from_stakeholders(
            STAKEHOLDERS,
            changed_paths=["src/app.py", "docs/api/reference.md"],
            pr_author_login="contributor",
        )
        self.assertEqual(reviewers, ["python-owner"])

    def test_excludes_pr_author_and_uses_next_matching_rule(self) -> None:
        reviewers = _deterministic_reviewer_from_stakeholders(
            STAKEHOLDERS,
            changed_paths=["docs/api/reference.md"],
            pr_author_login="api-owner",
        )
        self.assertEqual(reviewers, ["docs-owner"])

    def test_falls_back_to_first_eligible_roster_owner_when_no_path_matches(self) -> None:
        reviewers = _deterministic_reviewer_from_stakeholders(
            STAKEHOLDERS,
            changed_paths=["unknown/file.txt"],
            pr_author_login="contributor",
        )
        self.assertEqual(reviewers, ["fallback"])

    def test_returns_empty_when_no_eligible_owner_exists(self) -> None:
        reviewers = _deterministic_reviewer_from_stakeholders(
            [{"pattern": "*", "owners": ["contributor"]}],
            changed_paths=["anything.txt"],
            pr_author_login="contributor",
        )
        self.assertEqual(reviewers, [])


class ResolveRecommendedReviewersTest(unittest.TestCase):
    def test_accepts_single_agent_reviewer_from_stakeholders(self) -> None:
        reviewers = _resolve_recommended_reviewers(
            {"recommended_reviewers": ["@api-owner"]},
            stakeholder_entries=STAKEHOLDERS,
            changed_paths=["docs/api/reference.md"],
            pr_author_login="contributor",
        )
        self.assertEqual(reviewers, ["api-owner"])

    def test_falls_back_when_agent_returns_multiple_reviewers(self) -> None:
        reviewers = _resolve_recommended_reviewers(
            {"recommended_reviewers": ["docs-owner", "api-owner"]},
            stakeholder_entries=STAKEHOLDERS,
            changed_paths=["docs/api/reference.md"],
            pr_author_login="contributor",
        )
        self.assertEqual(reviewers, ["api-owner"])

    def test_falls_back_when_agent_reviewer_is_not_a_stakeholder(self) -> None:
        reviewers = _resolve_recommended_reviewers(
            {"recommended_reviewers": ["outsider"]},
            stakeholder_entries=STAKEHOLDERS,
            changed_paths=["docs/api/reference.md"],
            pr_author_login="contributor",
        )
        self.assertEqual(reviewers, ["api-owner"])

    def test_falls_back_when_agent_reviewer_is_pr_author(self) -> None:
        reviewers = _resolve_recommended_reviewers(
            {"recommended_reviewers": ["contributor"]},
            stakeholder_entries=STAKEHOLDERS,
            changed_paths=["docs/api/reference.md"],
            pr_author_login="contributor",
        )
        self.assertEqual(reviewers, ["api-owner"])

    def test_falls_back_when_reviewers_payload_is_not_a_list(self) -> None:
        reviewers = _resolve_recommended_reviewers(
            {"recommended_reviewers": "api-owner"},
            stakeholder_entries=STAKEHOLDERS,
            changed_paths=["docs/api/reference.md"],
            pr_author_login="contributor",
        )
        self.assertEqual(reviewers, ["api-owner"])

    def test_returns_empty_when_agent_reviewer_is_not_in_empty_stakeholders(self) -> None:
        reviewers = _resolve_recommended_reviewers(
            {"recommended_reviewers": ["api-owner"]},
            stakeholder_entries=[],
            changed_paths=["docs/api/reference.md"],
            pr_author_login="contributor",
        )
        self.assertEqual(reviewers, [])


class NonMemberPromptSectionTest(unittest.TestCase):
    def test_prompt_requires_single_reviewer_and_gates_on_verdict(self) -> None:
        prompt = _format_non_member_review_section(
            pr_author_login="contributor",
            stakeholders_block="- /docs/ → @docs-owner",
        )
        self.assertIn("exactly one bare GitHub login", prompt)
        self.assertIn("Do not return more than one reviewer", prompt)
        # The prompt should tie the reviewer request to ``verdict`` =
        # APPROVE and explicitly mention the REJECT → REQUEST_CHANGES
        # behavior so the agent understands when its reviewer choice
        # will actually be honored.
        self.assertIn("`verdict` is `\"APPROVE\"`", prompt)
        self.assertIn("REQUEST_CHANGES", prompt)


class FormatReviewCompletionMessageTest(unittest.TestCase):
    def test_comment_with_recommended_reviewer_mentions_them(self) -> None:
        message = _format_review_completion_message("COMMENT", ["alice"])
        self.assertIn("@alice", message)
        self.assertIn("requested human review", message)
        self.assertNotIn("I approved", message)

    def test_plain_comment_no_reviewers(self) -> None:
        message = _format_review_completion_message("COMMENT", [])
        self.assertIn("completed the review", message)
        self.assertNotIn("approved", message.lower())


class ParseVerdictTest(unittest.TestCase):
    def test_uppercase_approve(self) -> None:
        self.assertEqual(_parse_verdict({"verdict": "APPROVE"}), "APPROVE")

    def test_uppercase_reject(self) -> None:
        self.assertEqual(_parse_verdict({"verdict": "REJECT"}), "REJECT")

    def test_lowercase_is_normalized(self) -> None:
        self.assertEqual(_parse_verdict({"verdict": "approve"}), "APPROVE")
        self.assertEqual(_parse_verdict({"verdict": "reject"}), "REJECT")

    def test_surrounding_whitespace_is_stripped(self) -> None:
        self.assertEqual(_parse_verdict({"verdict": "  REJECT "}), "REJECT")

    def test_missing_verdict_defaults_to_approve(self) -> None:
        self.assertEqual(_parse_verdict({}), "APPROVE")

    def test_invalid_verdict_defaults_to_approve(self) -> None:
        self.assertEqual(_parse_verdict({"verdict": "maybe"}), "APPROVE")

    def test_non_string_verdict_defaults_to_approve(self) -> None:
        self.assertEqual(_parse_verdict({"verdict": 1}), "APPROVE")
        self.assertEqual(_parse_verdict({"verdict": None}), "APPROVE")


class ApplyReviewResultVerdictTest(unittest.TestCase):
    """Verify ``apply_review_result`` honors the agent-supplied verdict."""

    def _make_context(self, *, is_non_member: bool) -> dict:
        return {
            "owner": "acme",
            "repo": "widgets",
            "pr_number": 7,
            "requester": "alice",
            "is_non_member": is_non_member,
            "pr_author_login": "contributor",
            "stakeholder_entries": STAKEHOLDERS,
            "stakeholder_logins": ["api-owner", "docs-owner", "fallback", "python-owner"],
            "diff_line_map": {},
            "diff_content_map": {},
        }

    def _make_github(self, pr: MagicMock) -> MagicMock:
        github = MagicMock()
        github.get_pull.return_value = pr
        return github

    def test_reject_member_pr_emits_request_changes_without_reviewer_request(self) -> None:
        pr = MagicMock()
        github = self._make_github(pr)
        progress = MagicMock()
        apply_review_result(
            github,
            context=self._make_context(is_non_member=False),
            run=MagicMock(),
            result={"verdict": "REJECT", "summary": "Needs work", "comments": []},
            progress=progress,
        )
        pr.create_review.assert_called_once()
        kwargs = pr.create_review.call_args.kwargs
        self.assertEqual(kwargs["event"], "REQUEST_CHANGES")
        self.assertIn("Needs work", kwargs["body"])
        pr.create_review_request.assert_not_called()

    def test_reject_non_member_pr_skips_reviewer_request(self) -> None:
        pr = MagicMock()
        github = self._make_github(pr)
        progress = MagicMock()
        apply_review_result(
            github,
            context=self._make_context(is_non_member=True),
            run=MagicMock(),
            result={
                "verdict": "REJECT",
                "summary": "Needs work",
                "comments": [],
                "recommended_reviewers": ["api-owner"],
            },
            progress=progress,
        )
        pr.create_review.assert_called_once()
        self.assertEqual(
            pr.create_review.call_args.kwargs["event"], "REQUEST_CHANGES"
        )
        pr.create_review_request.assert_not_called()

    def test_approve_non_member_pr_requests_reviewers_and_uses_comment_event(self) -> None:
        pr = MagicMock()
        github = self._make_github(pr)
        progress = MagicMock()
        apply_review_result(
            github,
            context=self._make_context(is_non_member=True),
            run=MagicMock(),
            result={
                "verdict": "APPROVE",
                "summary": "Looks good",
                "comments": [],
                "recommended_reviewers": ["api-owner"],
            },
            progress=progress,
        )
        pr.create_review.assert_called_once()
        self.assertEqual(pr.create_review.call_args.kwargs["event"], "COMMENT")
        pr.create_review_request.assert_called_once_with(reviewers=["api-owner"])

    def test_approve_member_pr_uses_comment_event_without_reviewer_request(self) -> None:
        pr = MagicMock()
        github = self._make_github(pr)
        progress = MagicMock()
        apply_review_result(
            github,
            context=self._make_context(is_non_member=False),
            run=MagicMock(),
            result={"verdict": "APPROVE", "summary": "Looks good", "comments": []},
            progress=progress,
        )
        pr.create_review.assert_called_once()
        self.assertEqual(pr.create_review.call_args.kwargs["event"], "COMMENT")
        pr.create_review_request.assert_not_called()

    def test_reject_with_no_summary_still_posts_request_changes(self) -> None:
        pr = MagicMock()
        github = self._make_github(pr)
        progress = MagicMock()
        apply_review_result(
            github,
            context=self._make_context(is_non_member=False),
            run=MagicMock(),
            result={"verdict": "REJECT", "summary": "", "comments": []},
            progress=progress,
        )
        pr.create_review.assert_called_once()
        kwargs = pr.create_review.call_args.kwargs
        self.assertEqual(kwargs["event"], "REQUEST_CHANGES")
        # The placeholder body keeps the call valid for GitHub.
        self.assertIn("Automated review", kwargs["body"])

    def test_approve_with_no_feedback_short_circuits(self) -> None:
        pr = MagicMock()
        github = self._make_github(pr)
        progress = MagicMock()
        apply_review_result(
            github,
            context=self._make_context(is_non_member=False),
            run=MagicMock(),
            result={"verdict": "APPROVE", "summary": "", "comments": []},
            progress=progress,
        )
        pr.create_review.assert_not_called()
        pr.create_review_request.assert_not_called()
        progress.complete.assert_called_once()

    def test_approve_and_reject_use_identical_review_body_text(self) -> None:
        approve_pr = MagicMock()
        reject_pr = MagicMock()
        progress = MagicMock()
        apply_review_result(
            self._make_github(approve_pr),
            context=self._make_context(is_non_member=False),
            run=MagicMock(),
            result={"verdict": "APPROVE", "summary": "Looks good", "comments": []},
            progress=progress,
        )
        apply_review_result(
            self._make_github(reject_pr),
            context=self._make_context(is_non_member=False),
            run=MagicMock(),
            result={"verdict": "REJECT", "summary": "Looks good", "comments": []},
            progress=progress,
        )
        self.assertEqual(
            approve_pr.create_review.call_args.kwargs["body"],
            reject_pr.create_review.call_args.kwargs["body"],
        )


if __name__ == "__main__":
    unittest.main()
