"""Tests for deterministic single-reviewer selection in review_pr."""

from __future__ import annotations

import unittest

from . import conftest  # noqa: F401

from scripts.review_pr import (  # type: ignore[import-not-found]
    _deterministic_reviewer_from_stakeholders,
    _format_non_member_review_section,
    _format_review_completion_message,
    _resolve_recommended_reviewers,
    _stakeholder_pattern_matches,
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
    def test_prompt_requires_single_reviewer_and_forbids_review_actions(self) -> None:
        prompt = _format_non_member_review_section(
            pr_author_login="contributor",
            stakeholders_block="- /docs/ → @docs-owner",
        )
        self.assertIn("exactly one bare GitHub login", prompt)
        self.assertIn("Do not return more than one reviewer", prompt)
        self.assertIn("Do not emit any review-action field", prompt)


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


if __name__ == "__main__":
    unittest.main()
