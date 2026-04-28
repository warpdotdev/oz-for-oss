"""Tests for ``control_plane.lib.trust``."""

from __future__ import annotations

import unittest

from . import conftest  # noqa: F401

from lib.trust import (
    ORG_MEMBER_ASSOCIATIONS,
    evaluate_actor_trust,
    is_org_member_association,
)


class IsOrgMemberAssociationTest(unittest.TestCase):
    def test_recognizes_each_member_association(self) -> None:
        for association in ("OWNER", "MEMBER", "COLLABORATOR"):
            with self.subTest(association=association):
                self.assertTrue(is_org_member_association(association))

    def test_treats_lowercase_as_member(self) -> None:
        self.assertTrue(is_org_member_association("member"))

    def test_strips_surrounding_whitespace(self) -> None:
        self.assertTrue(is_org_member_association(" MEMBER "))

    def test_rejects_non_member_associations(self) -> None:
        for association in ("CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE", ""):
            with self.subTest(association=association):
                self.assertFalse(is_org_member_association(association))

    def test_rejects_non_string_values(self) -> None:
        for association in (None, 0, ["MEMBER"], {"role": "MEMBER"}):
            with self.subTest(association=association):
                self.assertFalse(is_org_member_association(association))

    def test_member_association_set_is_canonical(self) -> None:
        self.assertEqual(
            ORG_MEMBER_ASSOCIATIONS, {"OWNER", "MEMBER", "COLLABORATOR"}
        )


class EvaluateActorTrustTest(unittest.TestCase):
    def test_member_actor_trusted_without_probe(self) -> None:
        probe_calls: list[tuple[str, str]] = []

        def probe(*, org: str, login: str) -> int:
            probe_calls.append((org, login))
            return 404

        actor = {
            "user": {"login": "alice"},
            "author_association": "MEMBER",
        }
        self.assertTrue(evaluate_actor_trust(actor=actor, org="acme", probe=probe))
        self.assertEqual(probe_calls, [])

    def test_owner_actor_trusted_without_probe(self) -> None:
        actor = {"user": {"login": "owner"}, "author_association": "OWNER"}

        def probe(*, org: str, login: str) -> int:
            raise AssertionError("probe should not be invoked for OWNER")

        self.assertTrue(evaluate_actor_trust(actor=actor, org="acme", probe=probe))

    def test_contributor_promoted_via_membership_probe(self) -> None:
        probe_calls: list[tuple[str, str]] = []

        def probe(*, org: str, login: str) -> int:
            probe_calls.append((org, login))
            return 204

        actor = {
            "user": {"login": "alice"},
            "author_association": "CONTRIBUTOR",
        }
        self.assertTrue(evaluate_actor_trust(actor=actor, org="acme", probe=probe))
        self.assertEqual(probe_calls, [("acme", "alice")])

    def test_contributor_with_404_membership_probe_is_untrusted(self) -> None:
        def probe(*, org: str, login: str) -> int:
            return 404

        actor = {
            "user": {"login": "alice"},
            "author_association": "CONTRIBUTOR",
        }
        self.assertFalse(evaluate_actor_trust(actor=actor, org="acme", probe=probe))

    def test_probe_exception_falls_back_to_untrusted(self) -> None:
        def probe(*, org: str, login: str) -> int:
            raise RuntimeError("network down")

        actor = {
            "user": {"login": "alice"},
            "author_association": "CONTRIBUTOR",
        }
        self.assertFalse(evaluate_actor_trust(actor=actor, org="acme", probe=probe))

    def test_probe_returning_302_is_untrusted(self) -> None:
        # GitHub redirects to the public endpoint when the requester
        # cannot see private membership, which means the user is not a
        # public member. Treat the redirect as untrusted.
        def probe(*, org: str, login: str) -> int:
            return 302

        actor = {
            "user": {"login": "alice"},
            "author_association": "CONTRIBUTOR",
        }
        self.assertFalse(evaluate_actor_trust(actor=actor, org="acme", probe=probe))

    def test_missing_actor_is_untrusted(self) -> None:
        def probe(*, org: str, login: str) -> int:
            raise AssertionError("probe should not run on missing actor")

        for actor in (None, {}, "not an object"):
            with self.subTest(actor=actor):
                self.assertFalse(evaluate_actor_trust(actor=actor, org="acme", probe=probe))  # type: ignore[arg-type]

    def test_missing_login_is_untrusted(self) -> None:
        def probe(*, org: str, login: str) -> int:
            raise AssertionError("probe should not run without login")

        actor = {"user": {}, "author_association": "CONTRIBUTOR"}
        self.assertFalse(evaluate_actor_trust(actor=actor, org="acme", probe=probe))

    def test_missing_org_is_untrusted(self) -> None:
        def probe(*, org: str, login: str) -> int:
            raise AssertionError("probe should not run without org")

        actor = {"user": {"login": "alice"}, "author_association": "CONTRIBUTOR"}
        self.assertFalse(evaluate_actor_trust(actor=actor, org="", probe=probe))


if __name__ == "__main__":
    unittest.main()
