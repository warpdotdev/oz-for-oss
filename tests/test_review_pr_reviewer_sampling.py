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

from . import conftest  # noqa: F401

from scripts.review_pr import (  # type: ignore[import-not-found]
    _REVIEWER_SAMPLE_SIZE,
    _normalize_reviewer_logins,
    _resolve_non_member_review_action,
)


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


class ResolveNonMemberReviewActionTest(unittest.TestCase):
    def test_approve_yields_one_reviewer(self) -> None:
        review = {
            "verdict": "APPROVE",
            "recommended_reviewers": ["alice", "bob", "carol"],
        }
        event, reviewers = _resolve_non_member_review_action(
            review,
            pr_author_login="dave",
            allowed_logins={"alice", "bob", "carol"},
        )
        self.assertEqual(event, "APPROVE")
        self.assertEqual(len(reviewers), 1)
        self.assertIn(reviewers[0], {"alice", "bob", "carol"})

    def test_request_changes_returns_no_reviewers(self) -> None:
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

    def test_invalid_verdict_raises(self) -> None:
        with self.assertRaises(ValueError):
            _resolve_non_member_review_action(
                {"verdict": "COMMENT"},
                pr_author_login="dave",
            )


if __name__ == "__main__":
    unittest.main()
