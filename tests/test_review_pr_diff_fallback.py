"""Regression tests for REMOTE-2757.

GitHub's Files API omits ``patch`` for any file whose diff exceeds its
internal size threshold — not just binaries. When that happens, the review
workflow must fall back to the PR-level ``.diff`` media type instead of
dropping the file's content, and the apply-time validation maps must be
built from the exact annotated text the reviewing agent saw.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from . import conftest  # noqa: F401

from workflows.review_pr import (  # type: ignore[import-not-found]
    _fetch_full_pr_diff,
    _format_pr_diff,
    _split_unified_diff_by_file,
    gather_review_context,
)


# Mirrors the shape of command-signatures#321: one file large enough that
# the Files API drops `patch`, plus an ordinary file with a normal patch,
# in a single PR-level unified diff payload.
FULL_DIFF_WITH_LARGE_FILE = """diff --git a/json/ip.json b/json/ip.json
index 1111111..2222222 100644
--- a/json/ip.json
+++ b/json/ip.json
@@ -1,4 +1,4 @@
 {
-  "old": true,
+  "new": true,
   "keep": 1
 }
diff --git a/README.md b/README.md
index 3333333..4444444 100644
--- a/README.md
+++ b/README.md
@@ -1,2 +1,2 @@
-old readme
+new readme
"""

BINARY_ONLY_DIFF = """diff --git a/assets/logo.png b/assets/logo.png
index 5555555..6666666 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""


class _FakeRequester:
    """Stand-in for ``github.Requester.Requester`` used by PyGithub objects."""

    def __init__(self, *, status: int = 200, output: str = "", raises: bool = False):
        self.status = status
        self.output = output
        self.raises = raises
        self.calls: list[tuple[str, str, dict[str, str] | None]] = []

    def requestBlob(self, verb, url, headers=None, **_kwargs):
        self.calls.append((verb, url, headers))
        if self.raises:
            raise RuntimeError("boom")
        return self.status, {}, self.output


class SplitUnifiedDiffByFileTest(unittest.TestCase):
    def test_extracts_hunks_for_each_file(self) -> None:
        sections = _split_unified_diff_by_file(FULL_DIFF_WITH_LARGE_FILE)
        self.assertIn("json/ip.json", sections)
        self.assertIn("README.md", sections)
        self.assertTrue(sections["json/ip.json"].startswith("@@ -1,4 +1,4 @@"))
        self.assertIn('-  "old": true,', sections["json/ip.json"])
        self.assertIn('+  "new": true,', sections["json/ip.json"])

    def test_binary_only_sections_are_omitted(self) -> None:
        sections = _split_unified_diff_by_file(BINARY_ONLY_DIFF)
        self.assertEqual(sections, {})


class FormatPrDiffFallbackTest(unittest.TestCase):
    def test_backfills_missing_patch_from_full_diff_sections(self) -> None:
        files = [
            SimpleNamespace(
                filename="json/ip.json",
                previous_filename=None,
                status="modified",
                patch=None,
            )
        ]
        full_diff_sections = _split_unified_diff_by_file(FULL_DIFF_WITH_LARGE_FILE)

        result = _format_pr_diff(files, full_diff_sections=full_diff_sections)

        self.assertNotIn("(Patch unavailable from GitHub for this file.)", result)
        self.assertIn("[NEW:2]", result)
        self.assertIn("[OLD:2]", result)

    def test_binary_file_keeps_honest_placeholder(self) -> None:
        files = [
            SimpleNamespace(
                filename="assets/logo.png",
                previous_filename=None,
                status="modified",
                patch=None,
            )
        ]
        full_diff_sections = _split_unified_diff_by_file(BINARY_ONLY_DIFF)

        result = _format_pr_diff(files, full_diff_sections=full_diff_sections)

        self.assertIn("(Patch unavailable from GitHub for this file.)", result)
        self.assertNotIn("[NEW:", result)

    def test_present_patch_is_preferred_over_full_diff_sections(self) -> None:
        files = [
            SimpleNamespace(
                filename="README.md",
                previous_filename=None,
                status="modified",
                patch="@@ -1,1 +1,1 @@\n-direct patch\n+direct patch updated",
            )
        ]
        # Even if a (mismatched) fallback section exists, the Files API
        # patch always wins when present.
        full_diff_sections = {"README.md": "@@ -1,1 +1,1 @@\n-wrong\n+wrong"}

        result = _format_pr_diff(files, full_diff_sections=full_diff_sections)

        self.assertIn("direct patch updated", result)
        self.assertNotIn("wrong", result)


class FetchFullPrDiffTest(unittest.TestCase):
    def test_returns_diff_text_on_success(self) -> None:
        requester = _FakeRequester(status=200, output=FULL_DIFF_WITH_LARGE_FILE)
        pr = SimpleNamespace(requester=requester, url="https://api.github.com/repos/acme/widgets/pulls/321")

        result = _fetch_full_pr_diff(pr)

        self.assertEqual(result, FULL_DIFF_WITH_LARGE_FILE)
        verb, url, headers = requester.calls[0]
        self.assertEqual(verb, "GET")
        self.assertEqual(url, pr.url)
        self.assertEqual(headers, {"Accept": "application/vnd.github.v3.diff"})

    def test_returns_none_on_406_aggregate_diff_too_large(self) -> None:
        requester = _FakeRequester(status=406, output="")
        pr = SimpleNamespace(requester=requester, url="https://api.github.com/repos/acme/widgets/pulls/321")

        self.assertIsNone(_fetch_full_pr_diff(pr))

    def test_returns_none_on_exception(self) -> None:
        requester = _FakeRequester(raises=True)
        pr = SimpleNamespace(requester=requester, url="https://api.github.com/repos/acme/widgets/pulls/321")

        self.assertIsNone(_fetch_full_pr_diff(pr))

    def test_returns_none_when_pr_has_no_requester(self) -> None:
        pr = SimpleNamespace()

        self.assertIsNone(_fetch_full_pr_diff(pr))


class GatherReviewContextDiffFallbackTest(unittest.TestCase):
    """End-to-end: Files API `patch` null + `.diff` present -> real annotated lines."""

    def _make_pr(self, *, files, requester) -> SimpleNamespace:
        return SimpleNamespace(
            user=SimpleNamespace(login="maintainer", type="User"),
            author_association="MEMBER",
            title="fix: refresh generated fixture",
            body="",
            base=SimpleNamespace(ref="main"),
            head=SimpleNamespace(ref="feature"),
            get_files=lambda: list(files),
            requester=requester,
            url="https://api.github.com/repos/acme/widgets/pulls/321",
        )

    def test_missing_patch_is_backfilled_and_validation_maps_match(self) -> None:
        files = [
            SimpleNamespace(
                filename="json/ip.json",
                previous_filename=None,
                status="modified",
                patch=None,
            )
        ]
        requester = _FakeRequester(status=200, output=FULL_DIFF_WITH_LARGE_FILE)
        pr = self._make_pr(files=files, requester=requester)
        github = MagicMock()
        github.get_pull.return_value = pr

        with (
            patch("workflows.review_pr.resolve_issue_number_for_pr", return_value=None),
            patch("workflows.review_pr.repo_local_skill_path_for_dispatch", return_value=None),
            patch("workflows.review_pr.resolve_spec_context_for_pr_via_api", return_value={}),
        ):
            context = gather_review_context(
                github,
                owner="acme",
                repo="widgets",
                pr_number=321,
                trigger_source="pull_request",
                requester="alice",
                workspace_path=Path("/tmp"),
            )

        # The bot no longer refuses: the attached diff has real content.
        diff_text = context["pr_diff_text"]
        self.assertNotIn("(Patch unavailable from GitHub for this file.)", diff_text)
        self.assertIn("[NEW:2]", diff_text)

        # The .diff media type was actually requested, exactly once.
        self.assertEqual(len(requester.calls), 1)
        self.assertEqual(
            requester.calls[0][2], {"Accept": "application/vnd.github.v3.diff"}
        )

        # Apply-time validation maps match what the agent saw.
        diff_line_map = context["diff_line_map"]
        diff_content_map = context["diff_content_map"]
        self.assertIn("json/ip.json", diff_line_map)
        self.assertIn(2, diff_line_map["json/ip.json"]["RIGHT"])
        self.assertEqual(
            diff_content_map["json/ip.json"]["RIGHT"]["2"], '  "new": true,'
        )

    def test_no_fallback_fetch_when_all_patches_present(self) -> None:
        files = [
            SimpleNamespace(
                filename="README.md",
                previous_filename=None,
                status="modified",
                patch="@@ -1,1 +1,1 @@\n-old\n+new",
            )
        ]
        requester = _FakeRequester(status=200, output=FULL_DIFF_WITH_LARGE_FILE)
        pr = self._make_pr(files=files, requester=requester)
        github = MagicMock()
        github.get_pull.return_value = pr

        with (
            patch("workflows.review_pr.resolve_issue_number_for_pr", return_value=None),
            patch("workflows.review_pr.repo_local_skill_path_for_dispatch", return_value=None),
            patch("workflows.review_pr.resolve_spec_context_for_pr_via_api", return_value={}),
        ):
            gather_review_context(
                github,
                owner="acme",
                repo="widgets",
                pr_number=321,
                trigger_source="pull_request",
                requester="alice",
                workspace_path=Path("/tmp"),
            )

        # No file needed the fallback, so the extra request is never made.
        self.assertEqual(requester.calls, [])

    def test_binary_file_still_shows_placeholder_when_diff_only_has_binary_marker(
        self,
    ) -> None:
        files = [
            SimpleNamespace(
                filename="assets/logo.png",
                previous_filename=None,
                status="modified",
                patch=None,
            )
        ]
        requester = _FakeRequester(status=200, output=BINARY_ONLY_DIFF)
        pr = self._make_pr(files=files, requester=requester)
        github = MagicMock()
        github.get_pull.return_value = pr

        with (
            patch("workflows.review_pr.resolve_issue_number_for_pr", return_value=None),
            patch("workflows.review_pr.repo_local_skill_path_for_dispatch", return_value=None),
            patch("workflows.review_pr.resolve_spec_context_for_pr_via_api", return_value={}),
        ):
            context = gather_review_context(
                github,
                owner="acme",
                repo="widgets",
                pr_number=321,
                trigger_source="pull_request",
                requester="alice",
                workspace_path=Path("/tmp"),
            )

        self.assertIn(
            "(Patch unavailable from GitHub for this file.)", context["pr_diff_text"]
        )
        self.assertEqual(context["diff_line_map"], {})


if __name__ == "__main__":
    unittest.main()
