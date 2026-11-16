"""Tests for deterministic single-reviewer selection in review_pr."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from . import conftest  # noqa: F401

from github.GithubException import GithubException

from workflows.review_pr import (  # type: ignore[import-not-found]
    _deterministic_reviewer_from_stakeholders,
    _format_non_member_review_section,
    _format_review_completion_message,
    _is_team_slug,
    _parse_verdict,
    _resolve_recommended_reviewers,
    _reviewer_from_linked_issue_assignees,
    _reviewer_from_pr_assignee,
    _split_reviewers,
    _stakeholder_pattern_matches,
    _team_slug_only,
    apply_review_result,
    gather_review_context,
    review_payload_subset,
    RETRIGGER_HINT,
)
from oz.helpers import POWERED_BY_SUFFIX
from oz.ownership import OwnershipArea


STAKEHOLDERS = [
    {"pattern": "*", "owners": ["fallback"]},
    {"pattern": "/docs/", "owners": ["docs-owner"]},
    {"pattern": "/docs/api/", "owners": ["api-owner"]},
    {"pattern": "/src/*.py", "owners": ["python-owner"]},
]

STAKEHOLDERS_WITH_TEAM = [
    {"pattern": "/", "owners": ["warpdotdev/oss-maintainers"]},
    {"pattern": "/docs/", "owners": ["docs-owner"]},
]

OWNERSHIP_AREAS = [
    OwnershipArea(
        name="Docs API",
        owners=["api-owner", "backup-owner"],
        matches="API reference docs and generated documentation",
    ),
    OwnershipArea(
        name="General Docs",
        owners=["docs-owner"],
        matches="General documentation pages and guides",
    ),
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


class LinkedIssueAssigneeReviewerTest(unittest.TestCase):
    def test_returns_first_login(self) -> None:
        self.assertEqual(
            _reviewer_from_linked_issue_assignees(["triager", "other"]),
            ["triager"],
        )

    def test_returns_empty_when_no_logins(self) -> None:
        self.assertEqual(_reviewer_from_linked_issue_assignees([]), [])

    def test_approve_non_member_pr_prefers_linked_issue_assignee_over_ownership_area(self) -> None:
        pr = MagicMock()
        github = MagicMock()
        github.get_pull.return_value = pr
        progress = MagicMock()
        context = {
            "owner": "acme",
            "repo": "widgets",
            "pr_number": 7,
            "requester": "alice",
            "is_non_member": True,
            "requires_human_reviewer": True,
            "pr_author_login": "contributor",
            "stakeholder_entries": STAKEHOLDERS,
            "stakeholder_logins": ["api-owner", "docs-owner", "fallback"],
            "linked_issue_reviewer_logins": ["triager"],
            "ownership_areas": [
                {
                    "name": "Docs API",
                    "owners": ["api-owner"],
                    "matches": "API reference docs",
                }
            ],
            "ownership_areas_loaded": True,
            "diff_line_map": {},
            "diff_content_map": {},
        }
        with patch("workflows.review_pr._resolve_recommended_reviewers") as resolve:
            apply_review_result(
                github,
                context=context,
                run=MagicMock(),
                result={
                    "verdict": "APPROVE",
                    "summary": "Looks good",
                    "comments": [],
                    "recommended_area": "Docs API",
                },
                progress=progress,
            )
        resolve.assert_not_called()
        pr.create_review_request.assert_called_once_with(reviewers=["triager"])

    def test_gather_context_stores_linked_issue_assignees(self) -> None:
        pr = MagicMock()
        pr.get_files.return_value = [
            SimpleNamespace(
                filename="src/app.py",
                patch="+print('hi')",
                status="modified",
            )
        ]
        pr.user.login = "contributor"
        pr.user.type = "User"
        pr.author_association = "CONTRIBUTOR"
        pr.assignees = []
        pr.title = "feat: add app"
        pr.body = ""
        pr.base.ref = "main"
        pr.head.ref = "feature"
        issue = MagicMock()
        issue.assignees = [SimpleNamespace(login="triager")]
        github = MagicMock()
        github.get_pull.return_value = pr
        github.get_issue.return_value = issue

        with (
            patch("workflows.review_pr.resolve_issue_number_for_pr", return_value=42),
            patch("workflows.review_pr.repo_local_skill_path_for_dispatch", return_value=None),
            patch("workflows.review_pr.resolve_spec_context_for_pr_via_api", return_value={}),
            patch("workflows.review_pr.load_stakeholders_from_repo", return_value=STAKEHOLDERS),
            patch("workflows.review_pr.load_ownership_areas_from_repo") as load_ownership,
        ):
            context = gather_review_context(
                github,
                owner="acme",
                repo="widgets",
                pr_number=7,
                trigger_source="pull_request",
                requester="alice",
                workspace_path=MagicMock(),
                ownership_repo_handle=MagicMock(),
            )

        self.assertEqual(context["linked_issue_reviewer_logins"], ["triager"])


class OwnershipAreasResolveReviewerTest(unittest.TestCase):
    def test_returns_single_owner_without_random_choice(self) -> None:
        with patch("workflows.review_pr._RANDOM.choice") as choose:
            reviewers = _resolve_recommended_reviewers(
                {"recommended_area": "General Docs"},
                ownership_areas=OWNERSHIP_AREAS,
                repo_handle=MagicMock(),
                changed_paths=["docs/readme.md"],
                pr_author_login="contributor",
            )
        self.assertEqual(reviewers, ["docs-owner"])
        choose.assert_not_called()

    def test_falls_back_when_area_has_only_pr_author(self) -> None:
        repo_handle = MagicMock()
        with patch(
            "workflows.review_pr.load_stakeholders_from_repo",
            return_value=STAKEHOLDERS,
        ) as load:
            reviewers = _resolve_recommended_reviewers(
                {"recommended_area": "Runtime"},
                ownership_areas=[
                    OwnershipArea(
                        name="Runtime",
                        owners=["contributor"],
                        matches="Runtime internals",
                    )
                ],
                repo_handle=repo_handle,
                changed_paths=["docs/api/reference.md"],
                pr_author_login="contributor",
            )
        self.assertEqual(reviewers, ["api-owner"])
        load.assert_called_once_with(repo_handle)

    def test_empty_recommended_area_uses_lazy_stakeholders_fallback(self) -> None:
        repo_handle = MagicMock()
        with patch(
            "workflows.review_pr.load_stakeholders_from_repo",
            return_value=STAKEHOLDERS,
        ) as load:
            reviewers = _resolve_recommended_reviewers(
                {"recommended_area": ""},
                ownership_areas=OWNERSHIP_AREAS,
                repo_handle=repo_handle,
                changed_paths=["docs/api/reference.md"],
                pr_author_login="contributor",
            )
        self.assertEqual(reviewers, ["api-owner"])
        load.assert_called_once_with(repo_handle)

    def test_missing_invalid_or_unknown_area_uses_stakeholders_fallback(self) -> None:
        for payload in (
            {},
            {"recommended_area": 1},
            {"recommended_area": "Unknown Area"},
        ):
            with self.subTest(payload=payload):
                repo_handle = MagicMock()
                with patch(
                    "workflows.review_pr.load_stakeholders_from_repo",
                    return_value=STAKEHOLDERS,
                ) as load:
                    reviewers = _resolve_recommended_reviewers(
                        payload,
                        ownership_areas=OWNERSHIP_AREAS,
                        repo_handle=repo_handle,
                        changed_paths=["docs/api/reference.md"],
                        pr_author_login="contributor",
                    )
                self.assertEqual(reviewers, ["api-owner"])
                load.assert_called_once_with(repo_handle)

    def test_returns_empty_when_fallback_stakeholders_are_empty(self) -> None:
        repo_handle = MagicMock()
        with patch(
            "workflows.review_pr.load_stakeholders_from_repo",
            return_value=[],
        ) as load:
            reviewers = _resolve_recommended_reviewers(
                {"recommended_area": "Unknown Area"},
                ownership_areas=OWNERSHIP_AREAS,
                repo_handle=repo_handle,
                changed_paths=["docs/api/reference.md"],
                pr_author_login="contributor",
            )
        self.assertEqual(reviewers, [])
        load.assert_called_once_with(repo_handle)


class PrAssigneeReviewerTest(unittest.TestCase):
    def test_uses_first_eligible_assignee(self) -> None:
        pr = SimpleNamespace(
            assignees=[
                SimpleNamespace(login="contributor"),
                SimpleNamespace(login="assigned-owner"),
            ]
        )
        self.assertEqual(
            _reviewer_from_pr_assignee(pr, pr_author_login="contributor"),
            ["assigned-owner"],
        )

    def test_gather_context_skips_ownership_lookup_when_assignee_exists(self) -> None:
        pr = MagicMock()
        pr.get_files.return_value = [
            SimpleNamespace(
                filename="src/app.py",
                patch="+print('hi')",
                status="modified",
            )
        ]
        pr.user.login = "contributor"
        pr.user.type = "User"
        pr.author_association = "CONTRIBUTOR"
        pr.assignees = [SimpleNamespace(login="assigned-owner")]
        pr.title = "feat: add app"
        pr.body = ""
        pr.base.ref = "main"
        pr.head.ref = "feature"
        github = MagicMock()
        github.get_pull.return_value = pr

        with (
            patch("workflows.review_pr.resolve_issue_number_for_pr", return_value=None),
            patch("workflows.review_pr.repo_local_skill_path_for_dispatch", return_value=None),
            patch("workflows.review_pr.resolve_spec_context_for_pr_via_api", return_value={}),
            patch("workflows.review_pr.load_ownership_areas_from_repo") as load_ownership,
        ):
            context = gather_review_context(
                github,
                owner="acme",
                repo="widgets",
                pr_number=7,
                trigger_source="pull_request",
                requester="alice",
                workspace_path=MagicMock(),
                ownership_repo_handle=MagicMock(),
            )

        self.assertEqual(context["pr_assignee_reviewers"], ["assigned-owner"])
        self.assertFalse(context["ownership_areas_loaded"])
        self.assertEqual(context["non_member_review_section"], "")
        load_ownership.assert_not_called()


class OwnershipAreasPromptSectionTest(unittest.TestCase):
    def test_prompt_requires_single_area_and_gates_on_verdict(self) -> None:
        prompt = _format_non_member_review_section(
            pr_author_login="contributor",
            is_non_member=True,
            ownership_areas_block=(
                "- Docs API\n"
                "  owners: @api-owner, @backup-owner\n"
                "  matches: API reference docs and generated documentation"
            ),
            stakeholders_block="- /docs/ → @docs-owner",
            ownership_areas_loaded=True,
        )
        self.assertIn("recommended_area", prompt)
        self.assertIn("EXACTLY ONE area name", prompt)
        self.assertIn("Do NOT invent area names", prompt)
        self.assertIn("Do NOT return multiple names, a list, or owner handles", prompt)
        self.assertIn("empty string `\"\"`", prompt)
        self.assertIn("`verdict` is `\"APPROVE\"`", prompt)
        self.assertIn("REQUEST_CHANGES", prompt)
        self.assertIn("Fallback Stakeholders (from `.github/STAKEHOLDERS`)", prompt)
        self.assertIn("- /docs/ → @docs-owner", prompt)

    def test_fallback_prompt_keeps_legacy_stakeholders_contract(self) -> None:
        prompt = _format_non_member_review_section(
            pr_author_login="contributor",
            is_non_member=True,
            stakeholders_block="- /docs/ → @docs-owner",
            ownership_areas_loaded=False,
        )
        self.assertIn("recommended_reviewers", prompt)
        self.assertIn("exactly one bare GitHub login string", prompt)
        self.assertIn("Do not return more than one reviewer", prompt)
        self.assertIn(
            "deterministically choose a fallback reviewer from `.github/STAKEHOLDERS`",
            prompt,
        )

    def test_bot_pr_reject_posts_comment_not_request_changes(self) -> None:
        # The Oz bot's own PRs (is_non_member=False) must not advertise a
        # blocking REQUEST_CHANGES outcome, matching apply_review_result.
        for ownership_areas_loaded in (True, False):
            with self.subTest(ownership_areas_loaded=ownership_areas_loaded):
                prompt = _format_non_member_review_section(
                    pr_author_login="oz-for-oss[bot]",
                    is_non_member=False,
                    ownership_areas_block="- Docs API\n  owners: @api-owner",
                    stakeholders_block="- /docs/ → @docs-owner",
                    ownership_areas_loaded=ownership_areas_loaded,
                )
                self.assertNotIn("REQUEST_CHANGES", prompt)
                self.assertIn("`COMMENT`", prompt)


class FormatReviewCompletionMessageTest(unittest.TestCase):
    def test_comment_with_recommended_reviewer_mentions_them(self) -> None:
        message = _format_review_completion_message("COMMENT", ["alice"])
        self.assertIn("@alice", message)
        self.assertIn("requested human review", message)
        self.assertNotIn("I approved", message)

    def test_plain_comment_no_reviewers(self) -> None:
        message = _format_review_completion_message("COMMENT", [])
        self.assertIn("completed the review", message)
        self.assertIn("no human review was requested", message)
        self.assertNotIn("posted feedback", message)
        self.assertNotIn("approved", message.lower())

    def test_plain_comment_null_reviewers(self) -> None:
        message = _format_review_completion_message("COMMENT", None)
        self.assertIn("completed the review", message)
        self.assertIn("no human review was requested", message)
        self.assertNotIn("posted feedback", message)
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


class ReviewPayloadSubsetTest(unittest.TestCase):
    def test_drops_prompt_only_attachment_payload_fields(self) -> None:
        subset = review_payload_subset(
            {
                "owner": "acme",
                "repo": "widgets",
                "pr_body": "large untrusted PR body",
                "pr_description_text": "rendered PR description",
                "pr_diff_text": "annotated diff",
                "spec_context_text": "spec context",
                "repo_local_section": "local guidance",
                "non_member_review_section": "reviewer selection",
                "diff_line_map": {},
            }
        )

        self.assertEqual(
            subset,
            {
                "owner": "acme",
                "repo": "widgets",
                "diff_line_map": {},
            },
        )


class ApplyReviewResultVerdictTest(unittest.TestCase):
    """Verify ``apply_review_result`` honors the agent-supplied verdict."""

    def _make_context(
        self,
        *,
        is_non_member: bool,
        ownership_areas: list[dict[str, object]] | None = None,
    ) -> dict:
        return {
            "owner": "acme",
            "repo": "widgets",
            "pr_number": 7,
            "requester": "alice",
            "is_non_member": is_non_member,
            "pr_author_login": "contributor",
            "stakeholder_entries": STAKEHOLDERS,
            "stakeholder_logins": ["api-owner", "docs-owner", "fallback", "python-owner"],
            "ownership_areas": ownership_areas or [],
            "ownership_areas_loaded": bool(ownership_areas),
            "diff_line_map": {},
            "diff_content_map": {},
        }

    def _make_github(self, pr: MagicMock) -> MagicMock:
        github = MagicMock()
        github.get_pull.return_value = pr
        return github

    def _make_review(
        self,
        *,
        state: str,
        body: str,
        is_bot: bool = True,
    ) -> MagicMock:
        review = MagicMock()
        review.state = state
        review.body = body
        review.id = 123
        review.user.login = "oz-for-oss[bot]" if is_bot else "human-reviewer"
        review.user.type = "Bot" if is_bot else "User"
        return review

    def test_reject_member_pr_uses_comment_event_without_reviewer_request(self) -> None:
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
        self.assertEqual(kwargs["event"], "COMMENT")
        self.assertIn("Needs work", kwargs["body"])
        pr.create_review_request.assert_not_called()

    def test_reject_non_member_pr_skips_reviewer_request(self) -> None:
        pr = MagicMock()
        github = self._make_github(pr)
        progress = MagicMock()
        apply_review_result(
            github,
            context=self._make_context(
                is_non_member=True,
                ownership_areas=[
                    {
                        "name": "Docs API",
                        "owners": ["api-owner"],
                        "matches": "API reference docs and generated documentation",
                    }
                ],
            ),
            run=MagicMock(),
            result={
                "verdict": "REJECT",
                "summary": "Needs work",
                "comments": [],
                "recommended_area": "Docs API",
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
            context=self._make_context(
                is_non_member=True,
                ownership_areas=[
                    {
                        "name": "Docs API",
                        "owners": ["api-owner"],
                        "matches": "API reference docs and generated documentation",
                    }
                ],
            ),
            run=MagicMock(),
            result={
                "verdict": "APPROVE",
                "summary": "Looks good",
                "comments": [],
                "recommended_area": "Docs API",
            },
            progress=progress,
        )
        pr.create_review.assert_called_once()
        self.assertEqual(pr.create_review.call_args.kwargs["event"], "COMMENT")
        pr.create_review_request.assert_called_once_with(reviewers=["api-owner"])

    def test_approve_non_member_pr_prefers_existing_assignee(self) -> None:
        pr = MagicMock()
        pr.assignees = [SimpleNamespace(login="assigned-owner")]
        github = self._make_github(pr)
        progress = MagicMock()
        with patch("workflows.review_pr._resolve_recommended_reviewers") as resolve:
            apply_review_result(
                github,
                context=self._make_context(
                    is_non_member=True,
                    ownership_areas=[
                        {
                            "name": "Docs API",
                            "owners": ["api-owner"],
                            "matches": "API reference docs and generated documentation",
                        }
                    ],
                ),
                run=MagicMock(),
                result={
                    "verdict": "APPROVE",
                    "summary": "Looks good",
                    "comments": [],
                    "recommended_area": "Docs API",
                },
                progress=progress,
            )
        resolve.assert_not_called()
        pr.create_review.assert_called_once()
        pr.create_review_request.assert_called_once_with(reviewers=["assigned-owner"])

    def test_approve_non_member_pr_prefers_existing_requested_reviewer(self) -> None:
        pr = MagicMock()
        pr.requested_reviewers = [SimpleNamespace(login="requested-owner")]
        pr.assignees = [SimpleNamespace(login="assigned-owner")]
        github = self._make_github(pr)
        progress = MagicMock()
        with patch("workflows.review_pr._resolve_recommended_reviewers") as resolve:
            apply_review_result(
                github,
                context=self._make_context(
                    is_non_member=True,
                    ownership_areas=[
                        {
                            "name": "Docs API",
                            "owners": ["api-owner"],
                            "matches": "API reference docs and generated documentation",
                        }
                    ],
                ),
                run=MagicMock(),
                result={
                    "verdict": "APPROVE",
                    "summary": "Looks good",
                    "comments": [],
                    "recommended_area": "Docs API",
                },
                progress=progress,
            )
        resolve.assert_not_called()
        pr.create_review.assert_called_once()
        pr.create_review_request.assert_called_once_with(
            reviewers=["requested-owner"]
        )

    def test_approve_non_member_pr_prefers_existing_requested_team(self) -> None:
        pr = MagicMock()
        pr.requested_reviewers = []
        pr.requested_teams = [SimpleNamespace(slug="reviewers")]
        pr.assignees = [SimpleNamespace(login="assigned-owner")]
        github = self._make_github(pr)
        progress = MagicMock()
        with patch("workflows.review_pr._resolve_recommended_reviewers") as resolve:
            apply_review_result(
                github,
                context=self._make_context(
                    is_non_member=True,
                    ownership_areas=[
                        {
                            "name": "Docs API",
                            "owners": ["api-owner"],
                            "matches": "API reference docs and generated documentation",
                        }
                    ],
                ),
                run=MagicMock(),
                result={
                    "verdict": "APPROVE",
                    "summary": "Looks good",
                    "comments": [],
                    "recommended_area": "Docs API",
                },
                progress=progress,
            )
        resolve.assert_not_called()
        pr.create_review.assert_called_once()
        pr.create_review_request.assert_called_once_with(
            team_reviewers=["reviewers"]
        )

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

    def test_approve_dismisses_previous_oz_request_changes_review(self) -> None:
        stale_review = self._make_review(
            state="CHANGES_REQUESTED",
            body=f"Needs work\n\n{RETRIGGER_HINT}\n\n{POWERED_BY_SUFFIX}",
        )
        human_review = self._make_review(
            state="CHANGES_REQUESTED",
            body="Please address this human feedback.",
            is_bot=False,
        )
        dismissed_review = self._make_review(
            state="DISMISSED",
            body=f"Outdated\n\n{RETRIGGER_HINT}",
        )
        pr = MagicMock()
        pr.get_reviews.return_value = [
            human_review,
            stale_review,
            dismissed_review,
        ]
        github = self._make_github(pr)
        progress = MagicMock()
        apply_review_result(
            github,
            context=self._make_context(
                is_non_member=True,
                ownership_areas=[
                    {
                        "name": "Docs API",
                        "owners": ["api-owner"],
                        "matches": "API reference docs and generated documentation",
                    }
                ],
            ),
            run=MagicMock(),
            result={
                "verdict": "APPROVE",
                "summary": "Looks good",
                "comments": [],
                "recommended_area": "Docs API",
            },
            progress=progress,
        )
        stale_review.dismiss.assert_called_once()
        human_review.dismiss.assert_not_called()
        dismissed_review.dismiss.assert_not_called()

    def test_approve_without_feedback_still_dismisses_previous_oz_request_changes_review(self) -> None:
        stale_review = self._make_review(
            state="CHANGES_REQUESTED",
            body=f"Needs work\n\n{RETRIGGER_HINT}",
        )
        pr = MagicMock()
        pr.get_reviews.return_value = [stale_review]
        github = self._make_github(pr)
        progress = MagicMock()
        apply_review_result(
            github,
            context=self._make_context(is_non_member=False),
            run=MagicMock(),
            result={"verdict": "APPROVE", "summary": "", "comments": []},
            progress=progress,
        )
        stale_review.dismiss.assert_called_once()
        pr.create_review.assert_not_called()
        progress.complete.assert_called_once()

    def test_approve_dismisses_legacy_oz_review_hint_request_changes_review(self) -> None:
        # Reviews posted before the /oz-review -> /warp-agent-review rename
        # carry this literal hint text. Spelled out verbatim (rather than via
        # _LEGACY_RETRIGGER_HINT) so this test still fails if that
        # compatibility branch is ever edited out from under it.
        legacy_hint = (
            "Comment `/oz-review` on this pull request to retrigger a review "
            "(up to 3 times on the same pull request)."
        )
        stale_review = self._make_review(
            state="CHANGES_REQUESTED",
            body=f"Needs work\n\n{legacy_hint}",
        )
        human_review = self._make_review(
            state="CHANGES_REQUESTED",
            body=f"Please address this human feedback.\n\n{legacy_hint}",
            is_bot=False,
        )
        dismissed_review = self._make_review(
            state="DISMISSED",
            body=f"Outdated\n\n{legacy_hint}",
        )
        pr = MagicMock()
        pr.get_reviews.return_value = [
            human_review,
            stale_review,
            dismissed_review,
        ]
        github = self._make_github(pr)
        progress = MagicMock()
        apply_review_result(
            github,
            context=self._make_context(is_non_member=False),
            run=MagicMock(),
            result={"verdict": "APPROVE", "summary": "", "comments": []},
            progress=progress,
        )
        stale_review.dismiss.assert_called_once()
        human_review.dismiss.assert_not_called()
        dismissed_review.dismiss.assert_not_called()

    def test_approve_dismisses_legacy_powered_by_oz_request_changes_review(self) -> None:
        # Reviews posted before the footer switched from Oz to Warp still
        # carry this exact suffix. Spelled out verbatim so this test fails
        # if that compatibility branch is removed.
        legacy_suffix = "_Powered by [Oz](https://oz.warp.dev)_"
        stale_review = self._make_review(
            state="CHANGES_REQUESTED",
            body=f"Needs work\n\n{legacy_suffix}",
        )
        human_review = self._make_review(
            state="CHANGES_REQUESTED",
            body=f"Please address this human feedback.\n\n{legacy_suffix}",
            is_bot=False,
        )
        dismissed_review = self._make_review(
            state="DISMISSED",
            body=f"Outdated\n\n{legacy_suffix}",
        )
        pr = MagicMock()
        pr.get_reviews.return_value = [
            human_review,
            stale_review,
            dismissed_review,
        ]
        github = self._make_github(pr)
        progress = MagicMock()
        apply_review_result(
            github,
            context=self._make_context(is_non_member=False),
            run=MagicMock(),
            result={"verdict": "APPROVE", "summary": "", "comments": []},
            progress=progress,
        )
        stale_review.dismiss.assert_called_once()
        human_review.dismiss.assert_not_called()
        dismissed_review.dismiss.assert_not_called()

    def test_reject_member_pr_with_no_feedback_short_circuits(self) -> None:
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
        pr.create_review.assert_not_called()
        pr.create_review_request.assert_not_called()
        progress.complete.assert_called_once()

    def test_reject_non_member_pr_with_no_summary_still_posts_request_changes(self) -> None:
        pr = MagicMock()
        github = self._make_github(pr)
        progress = MagicMock()
        apply_review_result(
            github,
            context=self._make_context(is_non_member=True),
            run=MagicMock(),
            result={"verdict": "REJECT", "summary": "", "comments": []},
            progress=progress,
        )
        pr.create_review.assert_called_once()
        kwargs = pr.create_review.call_args.kwargs
        self.assertEqual(kwargs["event"], "REQUEST_CHANGES")
        # The placeholder body keeps the call valid for GitHub.
        self.assertIn("Automated review", kwargs["body"])
        self.assertIn(POWERED_BY_SUFFIX, kwargs["body"])

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

    def test_member_approve_and_reject_use_identical_review_body_text(self) -> None:
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


class TeamSlugDetectionTest(unittest.TestCase):
    def test_user_login_is_not_team(self) -> None:
        self.assertFalse(_is_team_slug("alice"))
        self.assertFalse(_is_team_slug("docs-owner"))

    def test_org_team_slug_is_team(self) -> None:
        self.assertTrue(_is_team_slug("warpdotdev/oss-maintainers"))
        self.assertTrue(_is_team_slug("acme/reviewers"))

    def test_team_slug_only_strips_org_prefix(self) -> None:
        self.assertEqual(_team_slug_only("warpdotdev/oss-maintainers"), "oss-maintainers")
        self.assertEqual(_team_slug_only("acme/reviewers"), "reviewers")

    def test_team_slug_only_on_plain_login_returns_login(self) -> None:
        self.assertEqual(_team_slug_only("alice"), "alice")


class SplitReviewersTest(unittest.TestCase):
    def test_all_users(self) -> None:
        users, teams = _split_reviewers(["alice", "bob"])
        self.assertEqual(users, ["alice", "bob"])
        self.assertEqual(teams, [])

    def test_all_teams(self) -> None:
        users, teams = _split_reviewers(["warpdotdev/oss-maintainers"])
        self.assertEqual(users, [])
        self.assertEqual(teams, ["oss-maintainers"])

    def test_mixed(self) -> None:
        users, teams = _split_reviewers(["alice", "warpdotdev/reviewers"])
        self.assertEqual(users, ["alice"])
        self.assertEqual(teams, ["reviewers"])

    def test_empty(self) -> None:
        users, teams = _split_reviewers([])
        self.assertEqual(users, [])
        self.assertEqual(teams, [])


class ApplyReviewResultTeamReviewerTest(unittest.TestCase):
    """Verify ``apply_review_result`` routes team slugs to ``team_reviewers``."""

    def _make_context(self, *, stakeholder_entries: list | None = None) -> dict:
        return {
            "owner": "acme",
            "repo": "widgets",
            "pr_number": 7,
            "requester": "alice",
            "is_non_member": True,
            "pr_author_login": "contributor",
            "stakeholder_entries": stakeholder_entries or STAKEHOLDERS_WITH_TEAM,
            "stakeholder_logins": [],
            "ownership_areas": [],
            "ownership_areas_loaded": False,
            "diff_line_map": {},
            "diff_content_map": {},
        }

    def _make_github(self, pr: MagicMock) -> MagicMock:
        github = MagicMock()
        github.get_pull.return_value = pr
        return github

    def test_team_slug_routed_to_team_reviewers_param(self) -> None:
        pr = MagicMock()
        github = self._make_github(pr)
        progress = MagicMock()
        stakeholders = self._make_context()["stakeholder_entries"]
        with patch(
            "workflows.review_pr.load_stakeholders_from_repo",
            return_value=stakeholders,
        ):
            apply_review_result(
                github,
                context=self._make_context(),
                run=MagicMock(),
                result={
                    "verdict": "APPROVE",
                    "summary": "Looks good",
                    "comments": [],
                    "recommended_area": "",
                },
                progress=progress,
            )
        pr.create_review_request.assert_called_once_with(
            team_reviewers=["oss-maintainers"]
        )

    def test_user_login_routed_to_reviewers_param(self) -> None:
        pr = MagicMock()
        github = self._make_github(pr)
        progress = MagicMock()
        stakeholders = [{"pattern": "/docs/", "owners": ["docs-owner"]}]
        with patch(
            "workflows.review_pr.load_stakeholders_from_repo",
            return_value=stakeholders,
        ):
            apply_review_result(
                github,
                context=self._make_context(stakeholder_entries=stakeholders),
                run=MagicMock(),
                result={
                    "verdict": "APPROVE",
                    "summary": "Looks good",
                    "comments": [],
                    "recommended_area": "",
                },
                progress=progress,
            )
        pr.create_review_request.assert_called_once_with(
            reviewers=["docs-owner"]
        )

    def test_progress_comment_omits_reviewers_on_api_failure(self) -> None:
        pr = MagicMock()
        pr.create_review_request.side_effect = GithubException(
            422, {"message": "Reviews may only be requested from collaborators"},
            headers={},
        )
        github = self._make_github(pr)
        progress = MagicMock()
        stakeholders = self._make_context()["stakeholder_entries"]
        with patch(
            "workflows.review_pr.load_stakeholders_from_repo",
            return_value=stakeholders,
        ):
            apply_review_result(
                github,
                context=self._make_context(),
                run=MagicMock(),
                result={
                    "verdict": "APPROVE",
                    "summary": "Looks good",
                    "comments": [],
                    "recommended_area": "",
                },
                progress=progress,
            )
        progress.complete.assert_called_once()
        message = progress.complete.call_args[0][0]
        self.assertNotIn("oss-maintainers", message)
        self.assertIn("completed the review", message)
        self.assertIn("no human review was requested", message)
        self.assertNotIn("posted feedback", message)

    def test_progress_comment_mentions_reviewers_on_success(self) -> None:
        pr = MagicMock()
        github = self._make_github(pr)
        progress = MagicMock()
        stakeholders = self._make_context()["stakeholder_entries"]
        with patch(
            "workflows.review_pr.load_stakeholders_from_repo",
            return_value=stakeholders,
        ):
            apply_review_result(
                github,
                context=self._make_context(),
                run=MagicMock(),
                result={
                    "verdict": "APPROVE",
                    "summary": "Looks good",
                    "comments": [],
                    "recommended_area": "",
                },
                progress=progress,
            )
        progress.complete.assert_called_once()
        message = progress.complete.call_args[0][0]
        self.assertIn("oss-maintainers", message)
        self.assertIn("requested human review", message)


class RequiresHumanReviewerEscalationTest(unittest.TestCase):
    """``requires_human_reviewer`` keys on author identity."""

    def _make_pr(
        self,
        *,
        login: str,
        user_type: str,
        association: str,
        filenames: list[str],
    ) -> SimpleNamespace:
        files = [
            SimpleNamespace(
                filename=name,
                previous_filename=None,
                status="added",
                patch=None,
            )
            for name in filenames
        ]
        return SimpleNamespace(
            user=SimpleNamespace(login=login, type=user_type),
            author_association=association,
            title="feat: change",
            body="body",
            base=SimpleNamespace(ref="main"),
            head=SimpleNamespace(ref="feature"),
            get_files=lambda: list(files),
        )

    def _gather(self, pr: SimpleNamespace) -> dict:
        github = MagicMock()
        github.get_pull.return_value = pr
        with patch.dict(
            os.environ, {"GH_APP_SLUG": "oz-for-oss"}
        ), patch(
            "workflows.review_pr.resolve_issue_number_for_pr", return_value=None
        ), patch(
            "workflows.review_pr.repo_local_skill_path_for_dispatch",
            return_value=None,
        ), patch(
            "workflows.review_pr.resolve_spec_context_for_pr_via_api",
            return_value={},
        ), patch(
            "workflows.review_pr.load_stakeholders_from_repo",
            return_value=STAKEHOLDERS,
        ):
            return gather_review_context(
                github,
                owner="acme",
                repo="widgets",
                pr_number=7,
                trigger_source="pull_request",
                requester="alice",
                workspace_path=Path("/tmp"),
            )

    def _apply_approve(self, context: dict) -> MagicMock:
        pr = MagicMock()
        github = MagicMock()
        github.get_pull.return_value = pr
        with patch(
            "workflows.review_pr.load_stakeholders_from_repo",
            return_value=STAKEHOLDERS,
        ):
            apply_review_result(
                github,
                context=context,
                run=MagicMock(),
                result={
                    "verdict": "APPROVE",
                    "summary": "Looks good",
                    "comments": [],
                    "recommended_area": "",
                },
                progress=MagicMock(),
            )
        return pr

    def test_bot_and_external_authors_request_reviewer_on_approve(self) -> None:
        cases = [
            ("oz-spec", "oz-for-oss[bot]", "Bot", "NONE", ["specs/GH1/product.md"]),
            ("oz-impl", "oz-for-oss[bot]", "Bot", "NONE", ["core/app.py"]),
            ("external-spec", "contributor", "User", "CONTRIBUTOR", ["specs/GH1/product.md"]),
            ("external-impl", "contributor", "User", "CONTRIBUTOR", ["core/app.py"]),
        ]
        for label, login, user_type, association, filenames in cases:
            with self.subTest(case=label):
                context = self._gather(
                    self._make_pr(
                        login=login,
                        user_type=user_type,
                        association=association,
                        filenames=filenames,
                    )
                )
                self.assertTrue(context["requires_human_reviewer"])
                self.assertTrue(context["non_member_review_section"])
                pr = self._apply_approve(context)
                pr.create_review_request.assert_called_once()

    def test_non_oz_bot_pr_does_not_require_human_reviewer(self) -> None:
        # dependabot and other automation accounts keep their own
        # reviewer-assignment conventions; only the configured Oz bot
        # escalates.
        context = self._gather(
            self._make_pr(
                login="dependabot[bot]",
                user_type="Bot",
                association="NONE",
                filenames=["requirements.txt"],
            )
        )
        self.assertFalse(context["requires_human_reviewer"])
        self.assertEqual(context["non_member_review_section"], "")
        pr = self._apply_approve(context)
        pr.create_review_request.assert_not_called()

    def test_member_pr_never_requires_human_reviewer(self) -> None:
        for filenames in (["specs/GH1/product.md"], ["core/app.py"]):
            with self.subTest(filenames=filenames):
                context = self._gather(
                    self._make_pr(
                        login="maintainer",
                        user_type="User",
                        association="MEMBER",
                        filenames=filenames,
                    )
                )
                self.assertFalse(context["requires_human_reviewer"])
                self.assertEqual(context["non_member_review_section"], "")
                pr = self._apply_approve(context)
                pr.create_review_request.assert_not_called()

    def test_oz_authored_reject_stays_comment_event(self) -> None:
        context = self._gather(
            self._make_pr(
                login="oz-for-oss[bot]",
                user_type="Bot",
                association="NONE",
                filenames=["core/app.py"],
            )
        )
        self.assertTrue(context["requires_human_reviewer"])
        self.assertFalse(context["is_non_member"])
        pr = MagicMock()
        github = MagicMock()
        github.get_pull.return_value = pr
        apply_review_result(
            github,
            context=context,
            run=MagicMock(),
            result={"verdict": "REJECT", "summary": "Needs work", "comments": []},
            progress=MagicMock(),
        )
        pr.create_review.assert_called_once()
        self.assertEqual(pr.create_review.call_args.kwargs["event"], "COMMENT")
        pr.create_review_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
