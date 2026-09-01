from __future__ import annotations

import unittest

from . import conftest  # noqa: F401

from oz.helpers import (
    POWERED_BY_SUFFIX,
    append_comment_sections,
    build_comment_body,
    comment_metadata,
)


_LEGACY_OZ_SUFFIX = "_Powered by [Oz](https://oz.warp.dev)_"


class BuildCommentBodyTest(unittest.TestCase):
    def test_appends_powered_by_warp_suffix(self) -> None:
        body = build_comment_body("hello", "")
        self.assertEqual(body, f"hello\n\n{POWERED_BY_SUFFIX}")
        self.assertEqual(POWERED_BY_SUFFIX, "_Powered by [Warp](https://www.warp.dev)_")

    def test_strips_current_suffix_before_reappending(self) -> None:
        body = build_comment_body(f"hello\n\n{POWERED_BY_SUFFIX}", "")
        self.assertEqual(body, f"hello\n\n{POWERED_BY_SUFFIX}")

    def test_strips_legacy_oz_suffix_and_replaces_with_current(self) -> None:
        # Comments posted before the footer switched from Oz to Warp still
        # carry this exact suffix. Spelled out verbatim so this test fails
        # if that compatibility strip is removed.
        metadata = comment_metadata("review-pull-request", 42, run_id="run-1")
        body = build_comment_body(f"hello\n\n{_LEGACY_OZ_SUFFIX}", metadata)
        self.assertEqual(body, f"hello\n\n{POWERED_BY_SUFFIX}\n\n{metadata}")
        self.assertNotIn(_LEGACY_OZ_SUFFIX, body)
        self.assertIn("<!-- oz-agent-metadata:", body)


class AppendCommentSectionsTest(unittest.TestCase):
    def test_drops_current_powered_by_section_then_reappends(self) -> None:
        existing = f"hello\n\n{POWERED_BY_SUFFIX}"
        body = append_comment_sections(existing, "", ["more"])
        self.assertEqual(body, f"hello\n\nmore\n\n{POWERED_BY_SUFFIX}")

    def test_drops_legacy_oz_powered_by_section_then_appends_current(self) -> None:
        existing = f"hello\n\n{_LEGACY_OZ_SUFFIX}"
        body = append_comment_sections(existing, "", ["more"])
        self.assertEqual(body, f"hello\n\nmore\n\n{POWERED_BY_SUFFIX}")
        self.assertNotIn(_LEGACY_OZ_SUFFIX, body)


if __name__ == "__main__":
    unittest.main()
