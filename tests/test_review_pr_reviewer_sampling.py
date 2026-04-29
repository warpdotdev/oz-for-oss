"""Tests for the random-single-reviewer selection behavior.

Issue #399 calls for assigning exactly one randomly-selected human
reviewer per non-member PR rather than every matching stakeholder.
The selection logic lives in
``lib.scripts.review_pr._normalize_reviewer_logins``; these tests
inject a deterministic :class:`random.Random` instance so the chosen
reviewer is predictable, and assert the surrounding filtering rules
(deduplication, PR-author exclusion, stakeholder gating) still hold.
"""

from __future__ import annotations

import random
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from . import conftest  # noqa: F401

from scripts.review_pr import (  # type: ignore[import-not-found]
    POWERED_BY_SUFFIX,
    _REVIEWER_SAMPLE_SIZE,
    RETRIGGER_HINT,
    _dismiss_management_app_request_changes_reviews,
    _fallback_reviewer_pool,
    _format_review_completion_message,
    _is_management_app_review,
    _normalize_reviewer_logins,
    _promote_approval_after_stale_request_changes_dismissal,
    _resolve_non_member_review_action,
)


def _existing_review(
    *,
    state: str = "REQUEST_CHANGES",
    login: str = "oz-management[bot]",
    user_type: str = "Bot",
    body: str | None = None,
    review_id: int = 123,
) -> Any:
    review = SimpleNamespace(
        id=review_id,
        state=state,
        user=SimpleNamespace(login=login, type=user_type),
        body=body if body is not None else f"Review body\n\n{RETRIGGER_HINT}",
    )
    review.dismiss = MagicMock()
    return review


class NormalizeReviewerLoginsTest(unittest.TestCase):
    def test_default_sample_size_is_one(self) -> None:
        # Lock in the production default: every non-member PR gets
        # exactly one reviewer requested, per issue #399.
        self.assertEqual(_REVIEWER_SAMPLE_SIZE, 1)

    def test_picks_exactly_one_login_from_pool(self) -> None:
        rng = random.Random(0)
        result = _normalize_reviewer_logins(
            ["alice", "bob", "carol"],
            pr_author_login="dave",
            rng=rng,
        )
        self.assertEqual(len(result), 1)
        self.assertIn(result[0], {"alice", "bob", "carol"})

    def test_uniform_random_distribution_over_pool(self) -> None:
        # Run many trials with a seeded RNG and confirm every eligible
        # candidate is selected at least once. This catches the obvious
        # regression where the helper falls back to "first eligible
        # candidate" semantics.
        rng = random.Random(42)
        candidates = ["alice", "bob", "carol", "dave"]
        seen_at_least_once: set[str] = set()
        for _ in range(200):
            result = _normalize_reviewer_logins(
                candidates,
                pr_author_login="elliot",
                rng=rng,
            )
            self.assertEqual(len(result), 1)
            seen_at_least_once.add(result[0])
        self.assertEqual(seen_at_least_once, set(candidates))

    def test_excludes_pr_author_from_pool(self) -> None:
        rng = random.Random(0)
        result = _normalize_reviewer_logins(
            ["alice", "bob"],
            pr_author_login="alice",
            rng=rng,
        )
        self.assertEqual(result, ["bob"])

    def test_excludes_pr_author_case_insensitively(self) -> None:
        rng = random.Random(0)
        result = _normalize_reviewer_logins(
            ["Alice", "bob"],
            pr_author_login="alice",
            rng=rng,
        )
        self.assertEqual(result, ["bob"])

    def test_dedupes_repeated_logins(self) -> None:
        rng = random.Random(0)
        # When all duplicates collapse to a single eligible login the
        # helper returns that login regardless of the RNG.
        result = _normalize_reviewer_logins(
            ["alice", "alice", "alice"],
            pr_author_login="bob",
            rng=rng,
        )
        self.assertEqual(result, ["alice"])

    def test_strips_at_prefix_and_blanks(self) -> None:
        rng = random.Random(0)
        result = _normalize_reviewer_logins(
            ["", "@alice", "  @bob  ", None, 42],  # type: ignore[list-item]
            pr_author_login="dave",
            rng=rng,
        )
        self.assertEqual(len(result), 1)
        self.assertIn(result[0], {"alice", "bob"})

    def test_filters_to_allowed_logins_set(self) -> None:
        rng = random.Random(0)
        result = _normalize_reviewer_logins(
            ["alice", "bob", "outsider"],
            pr_author_login="dave",
            allowed_logins={"alice", "bob"},
            rng=rng,
        )
        self.assertEqual(len(result), 1)
        self.assertIn(result[0], {"alice", "bob"})

    def test_returns_empty_when_pool_is_empty(self) -> None:
        rng = random.Random(0)
        result = _normalize_reviewer_logins(
            ["alice"],
            pr_author_login="alice",  # excluded as PR author
            rng=rng,
        )
        self.assertEqual(result, [])

    def test_returns_empty_for_non_list_candidates(self) -> None:
        rng = random.Random(0)
        result = _normalize_reviewer_logins(
            "alice",  # type: ignore[arg-type]
            pr_author_login="bob",
            rng=rng,
        )
        self.assertEqual(result, [])

    def test_returns_empty_when_sample_size_is_zero(self) -> None:
        rng = random.Random(0)
        result = _normalize_reviewer_logins(
            ["alice", "bob"],
            pr_author_login="dave",
            sample_size=0,
            rng=rng,
        )
        self.assertEqual(result, [])

    def test_pool_smaller_than_sample_returns_full_pool_shuffled(self) -> None:
        rng = random.Random(0)
        result = _normalize_reviewer_logins(
            ["alice", "bob"],
            pr_author_login="dave",
            sample_size=5,
            rng=rng,
        )
        self.assertEqual(set(result), {"alice", "bob"})
        self.assertEqual(len(result), 2)

    def test_explicit_larger_sample_size(self) -> None:
        # Sanity check: when callers explicitly request more than one
        # reviewer (e.g. a future configurable cap), the helper still
        # samples without replacement from the eligible pool.
        rng = random.Random(7)
        result = _normalize_reviewer_logins(
            ["alice", "bob", "carol", "dave"],
            pr_author_login="elliot",
            sample_size=2,
            rng=rng,
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(len(set(result)), 2)
        self.assertTrue(set(result).issubset({"alice", "bob", "carol", "dave"}))


class FallbackReviewerPoolTest(unittest.TestCase):
    def test_fallback_excludes_pr_author_and_samples_from_stakeholders(self) -> None:
        rng = random.Random(0)
        result = _fallback_reviewer_pool(
            {"alice", "bob", "carol"},
            pr_author_login="alice",
            rng=rng,
        )
        self.assertEqual(len(result), 1)
        self.assertIn(result[0], {"bob", "carol"})
        self.assertNotIn("alice", result)

    def test_fallback_excludes_pr_author_case_insensitively(self) -> None:
        rng = random.Random(0)
        result = _fallback_reviewer_pool(
            {"alice", "bob"},
            pr_author_login="ALICE",
            rng=rng,
        )
        self.assertEqual(result, ["bob"])

    def test_fallback_returns_empty_when_no_eligible_stakeholder_exists(self) -> None:
        rng = random.Random(0)
        self.assertEqual(
            _fallback_reviewer_pool(set(), pr_author_login="alice", rng=rng),
            [],
        )
        self.assertEqual(
            _fallback_reviewer_pool({"alice"}, pr_author_login="alice", rng=rng),
            [],
        )


class ResolveNonMemberReviewActionTest(unittest.TestCase):
    def test_approve_verdict_is_downgraded_to_comment_event(self) -> None:
        # The bot only ever takes ``REQUEST_CHANGES`` actions on PRs;
        # an ``APPROVE`` verdict from the agent is posted as a plain
        # ``COMMENT`` review so a human still has to actually approve.
        review = {
            "verdict": "APPROVE",
            "recommended_reviewers": ["alice", "bob", "carol"],
        }
        event, reviewers = _resolve_non_member_review_action(
            review,
            pr_author_login="dave",
            allowed_logins={"alice", "bob", "carol"},
        )
        self.assertEqual(event, "COMMENT")
        # The reviewer-request payload is preserved on APPROVE so the
        # workflow can still ping a human reviewer.
        self.assertEqual(len(reviewers), 1)
        self.assertIn(reviewers[0], {"alice", "bob", "carol"})

    def test_approve_with_empty_recommendations_falls_back_to_stakeholder_roster(
        self,
    ) -> None:
        rng = random.Random(0)
        review = {
            "verdict": "APPROVE",
            "recommended_reviewers": [],
        }
        event, reviewers = _resolve_non_member_review_action(
            review,
            pr_author_login="dave",
            allowed_logins={"alice", "bob", "carol"},
            rng=rng,
        )
        self.assertEqual(event, "COMMENT")
        self.assertEqual(len(reviewers), 1)
        self.assertIn(reviewers[0], {"alice", "bob", "carol"})

    def test_approve_with_omitted_recommendations_falls_back_to_stakeholder_roster(
        self,
    ) -> None:
        rng = random.Random(0)
        event, reviewers = _resolve_non_member_review_action(
            {"verdict": "APPROVE"},
            pr_author_login="dave",
            allowed_logins={"alice", "bob", "carol"},
            rng=rng,
        )
        self.assertEqual(event, "COMMENT")
        self.assertEqual(len(reviewers), 1)
        self.assertIn(reviewers[0], {"alice", "bob", "carol"})

    def test_approve_with_invalid_self_and_non_stakeholder_candidates_falls_back(
        self,
    ) -> None:
        rng = random.Random(0)
        review = {
            "verdict": "APPROVE",
            "recommended_reviewers": ["@dave", "outsider", "", None],
        }
        event, reviewers = _resolve_non_member_review_action(
            review,  # type: ignore[arg-type]
            pr_author_login="dave",
            allowed_logins={"alice", "bob"},
            rng=rng,
        )
        self.assertEqual(event, "COMMENT")
        self.assertEqual(len(reviewers), 1)
        self.assertIn(reviewers[0], {"alice", "bob"})

    def test_approve_with_no_allowed_logins_returns_no_reviewers(self) -> None:
        rng = random.Random(0)
        review = {
            "verdict": "APPROVE",
            "recommended_reviewers": [],
        }
        event, reviewers = _resolve_non_member_review_action(
            review,
            pr_author_login="dave",
            allowed_logins=set(),
            rng=rng,
        )
        self.assertEqual(event, "COMMENT")
        self.assertEqual(reviewers, [])

    def test_request_changes_returns_real_request_changes_event(self) -> None:
        # ``REQUEST_CHANGES`` is the one verdict that does become a
        # real GitHub review action on the PR.
        review = {
            "verdict": "REQUEST_CHANGES",
            "recommended_reviewers": ["alice", "bob"],
        }
        event, reviewers = _resolve_non_member_review_action(
            review,
            pr_author_login="dave",
            allowed_logins={"alice", "bob"},
        )
        self.assertEqual(event, "REQUEST_CHANGES")
        self.assertEqual(reviewers, [])

    def test_request_changes_does_not_fallback_to_stakeholder_roster(self) -> None:
        rng = random.Random(0)
        review = {
            "verdict": "REQUEST_CHANGES",
            "recommended_reviewers": [],
        }
        event, reviewers = _resolve_non_member_review_action(
            review,
            pr_author_login="dave",
            allowed_logins={"alice", "bob"},
            rng=rng,
        )
        self.assertEqual(event, "REQUEST_CHANGES")
        self.assertEqual(reviewers, [])

    def test_invalid_verdict_raises(self) -> None:
        with self.assertRaises(ValueError):
            _resolve_non_member_review_action(
                {"verdict": "COMMENT"},
                pr_author_login="dave",
            )


class ManagementAppReviewDismissalTest(unittest.TestCase):
    def test_identifies_configured_management_app_login(self) -> None:
        review = _existing_review(
            login="oz-management[bot]",
            user_type="Bot",
            body="plain review body",
        )
        self.assertTrue(
            _is_management_app_review(
                review,
                management_app_logins={"oz-management[bot]"},
            )
        )

    def test_identifies_signed_bot_review_without_configured_login(self) -> None:
        review_with_retrigger_hint = _existing_review(body=RETRIGGER_HINT)
        review_with_powered_by = _existing_review(body=POWERED_BY_SUFFIX)
        self.assertTrue(
            _is_management_app_review(
                review_with_retrigger_hint,
                management_app_logins=set(),
            )
        )
        self.assertTrue(
            _is_management_app_review(
                review_with_powered_by,
                management_app_logins=set(),
            )
        )

    def test_does_not_identify_human_review_without_configured_login(self) -> None:
        review = _existing_review(
            login="alice",
            user_type="User",
            body=RETRIGGER_HINT,
        )
        self.assertFalse(_is_management_app_review(review, management_app_logins=set()))

    def test_dismisses_only_matching_request_changes_reviews(self) -> None:
        stale_management_review = _existing_review(
            state="REQUEST_CHANGES",
            login="oz-management[bot]",
            body="plain management review body",
            review_id=1,
        )
        stale_human_review = _existing_review(
            state="REQUEST_CHANGES",
            login="alice",
            user_type="User",
            body="human review body",
            review_id=2,
        )
        already_approved_management_review = _existing_review(
            state="APPROVED",
            login="oz-management[bot]",
            body="plain management review body",
            review_id=3,
        )
        pr = SimpleNamespace(
            get_reviews=MagicMock(
                return_value=[
                    stale_management_review,
                    stale_human_review,
                    already_approved_management_review,
                ]
            )
        )
        dismissed = _dismiss_management_app_request_changes_reviews(
            pr,  # type: ignore[arg-type]
            management_app_logins={"oz-management[bot]"},
        )
        self.assertEqual(dismissed, 1)
        stale_management_review.dismiss.assert_called_once()
        stale_human_review.dismiss.assert_not_called()
        already_approved_management_review.dismiss.assert_not_called()

    def test_promotes_approve_verdict_after_dismissing_stale_review(self) -> None:
        stale_management_review = _existing_review(
            state="REQUEST_CHANGES",
            login="oz-management[bot]",
            body="plain management review body",
        )
        pr = SimpleNamespace(
            get_reviews=MagicMock(return_value=[stale_management_review])
        )
        event, dismissed = _promote_approval_after_stale_request_changes_dismissal(
            pr,  # type: ignore[arg-type]
            review={"verdict": "APPROVE"},
            event="COMMENT",
            management_app_logins={"oz-management[bot]"},
        )
        self.assertEqual(event, "APPROVE")
        self.assertEqual(dismissed, 1)
        stale_management_review.dismiss.assert_called_once()

    def test_does_not_promote_without_stale_matching_review(self) -> None:
        human_review = _existing_review(
            state="REQUEST_CHANGES",
            login="alice",
            user_type="User",
            body="human review body",
        )
        pr = SimpleNamespace(get_reviews=MagicMock(return_value=[human_review]))
        event, dismissed = _promote_approval_after_stale_request_changes_dismissal(
            pr,  # type: ignore[arg-type]
            review={"verdict": "APPROVE"},
            event="COMMENT",
            management_app_logins={"oz-management[bot]"},
        )
        self.assertEqual(event, "COMMENT")
        self.assertEqual(dismissed, 0)
        human_review.dismiss.assert_not_called()

    def test_does_not_promote_non_approve_verdict(self) -> None:
        stale_management_review = _existing_review(
            state="REQUEST_CHANGES",
            login="oz-management[bot]",
            body="plain management review body",
        )
        pr = SimpleNamespace(
            get_reviews=MagicMock(return_value=[stale_management_review])
        )
        event, dismissed = _promote_approval_after_stale_request_changes_dismissal(
            pr,  # type: ignore[arg-type]
            review={"verdict": "REQUEST_CHANGES"},
            event="REQUEST_CHANGES",
            management_app_logins={"oz-management[bot]"},
        )
        self.assertEqual(event, "REQUEST_CHANGES")
        self.assertEqual(dismissed, 0)
        stale_management_review.dismiss.assert_not_called()


class FormatReviewCompletionMessageTest(unittest.TestCase):
    def test_request_changes_message(self) -> None:
        message = _format_review_completion_message("REQUEST_CHANGES", [])
        self.assertIn("requested changes", message)
        self.assertNotIn("approve", message.lower())

    def test_comment_with_recommended_reviewers_mentions_them(self) -> None:
        # ``COMMENT`` event with reviewers attached means the original
        # verdict was ``APPROVE`` and the workflow downgraded it. The
        # progress comment should call out who got pinged for human
        # review and explicitly state that approval has to come from a
        # maintainer.
        message = _format_review_completion_message("COMMENT", ["alice"])
        self.assertIn("@alice", message)
        self.assertIn("maintainer can approve", message)
        # Must NOT claim that the bot itself approved the PR.
        self.assertNotIn("I approved", message)

    def test_plain_comment_no_reviewers(self) -> None:
        message = _format_review_completion_message("COMMENT", [])
        self.assertIn("completed the review", message)
        # Must NOT claim that the bot itself approved the PR.
        self.assertNotIn("approved", message.lower())

    def test_approve_message(self) -> None:
        message = _format_review_completion_message("APPROVE", ["alice"])
        self.assertIn("I approved this pull request", message)
        self.assertNotIn("maintainer can approve", message)


if __name__ == "__main__":
    unittest.main()
