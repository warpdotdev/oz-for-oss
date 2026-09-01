"""Tests for comment and PR body helpers in ``oz.helpers``.

Focused on the Powered-by footer: new Warp branding, and cleanup of the
pre-rebrand Oz suffix so mid-run edits do not leave a double footer.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from . import conftest  # noqa: F401

from oz.helpers import (
    POWERED_BY_SUFFIX,
    append_comment_sections,
    build_comment_body,
    build_pr_body,
)

# Spelled out verbatim rather than imported so these tests fail if
# suffix-stripping of the old Oz footer is edited out from under them.
_LEGACY_OZ_SUFFIX = "_Powered by [Oz](https://oz.warp.dev)_"


class BuildCommentBodyTest(unittest.TestCase):
    def test_appends_powered_by_warp_suffix(self) -> None:
        body = build_comment_body("I'm starting a first review of this pull request.", "")
        self.assertTrue(body.endswith(POWERED_BY_SUFFIX))
        self.assertIn("https://warp.dev", body)
        self.assertNotIn("oz.warp.dev", body)
        self.assertEqual(body.count("Powered by"), 1)

    def test_rebuild_does_not_duplicate_warp_suffix(self) -> None:
        body = build_comment_body("Stage 1", "")
        rebuilt = build_comment_body(body, "")
        self.assertEqual(body, rebuilt)
        self.assertEqual(rebuilt.count("Powered by"), 1)

    def test_rebuild_replaces_legacy_oz_suffix(self) -> None:
        body = build_comment_body(f"Stage 1\n\n{_LEGACY_OZ_SUFFIX}", "")
        self.assertTrue(body.endswith(POWERED_BY_SUFFIX))
        self.assertNotIn(_LEGACY_OZ_SUFFIX, body)
        self.assertEqual(body.count("Powered by"), 1)

    def test_preserves_metadata_after_suffix(self) -> None:
        metadata = '<!-- oz-agent-metadata: {"type":"issue-status","workflow":"review-pull-request","issue":42} -->'
        body = build_comment_body("Stage 1", metadata)
        self.assertTrue(body.endswith(metadata))
        self.assertIn(POWERED_BY_SUFFIX, body)
        self.assertLess(body.find(POWERED_BY_SUFFIX), body.find(metadata))


class AppendCommentSectionsTest(unittest.TestCase):
    def test_appends_section_and_keeps_single_warp_suffix(self) -> None:
        existing = build_comment_body("I'm starting a first review of this pull request.", "")
        updated = append_comment_sections(
            existing,
            "",
            ["You can follow along in [the session on Warp](https://example.test/session)."],
        )
        self.assertIn("I'm starting a first review", updated)
        self.assertIn("You can follow along", updated)
        self.assertTrue(updated.endswith(POWERED_BY_SUFFIX))
        self.assertEqual(updated.count("Powered by"), 1)

    def test_strips_legacy_oz_suffix_when_appending(self) -> None:
        existing = f"I'm starting a first review of this pull request.\n\n{_LEGACY_OZ_SUFFIX}"
        updated = append_comment_sections(
            existing,
            "",
            ["You can follow along in [the session on Warp](https://example.test/session)."],
        )
        self.assertNotIn(_LEGACY_OZ_SUFFIX, updated)
        self.assertTrue(updated.endswith(POWERED_BY_SUFFIX))
        self.assertEqual(updated.count("Powered by"), 1)


class BuildPrBodyTest(unittest.TestCase):
    def test_appends_powered_by_warp_suffix(self) -> None:
        github = MagicMock()
        github.compare.return_value = SimpleNamespace(commits=[])
        body = build_pr_body(
            github,
            "acme",
            "widgets",
            issue_number=42,
            head="feature",
            base="main",
        )
        self.assertIn("Closes #42", body)
        self.assertTrue(body.endswith(POWERED_BY_SUFFIX))
        self.assertNotIn("oz.warp.dev", body)
        self.assertEqual(body.count("Powered by"), 1)
