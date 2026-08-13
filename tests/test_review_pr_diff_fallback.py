"""Regression tests for REMOTE-2757.

GitHub's Files API omits ``patch`` for any file whose diff exceeds its
internal size threshold — not just binaries. When that happens, the review
workflow must fall back to the PR-level ``.diff`` media type instead of
dropping the file's content, and the apply-time validation maps must be
built from the exact annotated text the reviewing agent saw.

Also covers two follow-up review findings on the fallback parser:
- ``diff --git`` headers may quote and C-escape paths (spaces, tabs,
  non-UTF-8 bytes), which must decode to the same string as
  ``file.filename`` from the Files API.
- No-hunk sections (mode-only changes, empty-file add/delete) must still
  surface their extended-header lines instead of being dropped entirely.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from . import conftest  # noqa: F401

from workflows.review_pr import (  # type: ignore[import-not-found]
    _DiffFileSection,
    _fetch_full_pr_diff,
    _format_pr_diff,
    _split_unified_diff_by_file,
    _unquote_git_diff_path,
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

MODE_ONLY_DIFF = """diff --git a/scripts/deploy.sh b/scripts/deploy.sh
old mode 100644
new mode 100755
"""

EMPTY_FILE_ADD_DIFF = """diff --git a/newfile.txt b/newfile.txt
new file mode 100644
index 0000000..e69de29
"""

EMPTY_FILE_DELETE_DIFF = """diff --git a/removed.txt b/removed.txt
deleted file mode 100644
index e69de29..0000000
"""

QUOTED_SPACE_DIFF = """diff --git "a/docs/release notes.md" "b/docs/release notes.md"
index 1111111..2222222 100644
--- "a/docs/release notes.md"
+++ "b/docs/release notes.md"
@@ -1,1 +1,1 @@
-old
+new
"""

# A path containing a literal tab, C-escaped by Git as \t.
ESCAPED_TAB_DIFF = (
    'diff --git "a/weird\\tname.txt" "b/weird\\tname.txt"\n'
    "index 1111111..2222222 100644\n"
    '--- "a/weird\\tname.txt"\n'
    '+++ "b/weird\\tname.txt"\n'
    "@@ -1,1 +1,1 @@\n"
    "-old\n"
    "+new\n"
)

QUOTED_RENAME_DIFF = """diff --git "a/old dir/f.txt" "b/new dir/f.txt"
similarity index 100%
rename from "old dir/f.txt"
rename to "new dir/f.txt"
"""

COPY_DIFF = """diff --git a/orig.json b/copy.json
similarity index 100%
copy from orig.json
copy to copy.json
"""

ADDED_FILE_WITH_DEV_NULL_DIFF = """diff --git a/new_module.py b/new_module.py
new file mode 100644
index 0000000..abcdef0
--- /dev/null
+++ b/new_module.py
@@ -0,0 +1,2 @@
+line one
+line two
"""

DELETED_FILE_WITH_DEV_NULL_DIFF = """diff --git a/old_module.py b/old_module.py
deleted file mode 100644
index abcdef0..0000000
--- a/old_module.py
+++ /dev/null
@@ -1,2 +0,0 @@
-line one
-line two
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


class UnquoteGitDiffPathTest(unittest.TestCase):
    def test_plain_token_is_unchanged(self) -> None:
        self.assertEqual(_unquote_git_diff_path("a/foo.py"), "a/foo.py")

    def test_quoted_space_is_unquoted(self) -> None:
        self.assertEqual(
            _unquote_git_diff_path('"a/foo bar.txt"'), "a/foo bar.txt"
        )

    def test_c_escape_letters_decode(self) -> None:
        self.assertEqual(_unquote_git_diff_path('"a/tab\\tname"'), "a/tab\tname")
        self.assertEqual(_unquote_git_diff_path('"a/quote\\"name"'), 'a/quote"name')
        self.assertEqual(_unquote_git_diff_path('"a/back\\\\slash"'), "a/back\\slash")

    def test_octal_escape_decodes_non_ascii_byte(self) -> None:
        # "é" is 0xC3 0xA9 in UTF-8; git emits \303\251 with core.quotePath on.
        self.assertEqual(_unquote_git_diff_path('"a/caf\\303\\251.txt"'), "a/café.txt")


class SplitUnifiedDiffByFileTest(unittest.TestCase):
    def test_extracts_hunks_for_each_file(self) -> None:
        sections = _split_unified_diff_by_file(FULL_DIFF_WITH_LARGE_FILE)
        self.assertIn("json/ip.json", sections)
        self.assertIn("README.md", sections)
        self.assertTrue(
            sections["json/ip.json"].hunk_text.startswith("@@ -1,4 +1,4 @@")
        )
        self.assertIn('-  "old": true,', sections["json/ip.json"].hunk_text)
        self.assertIn('+  "new": true,', sections["json/ip.json"].hunk_text)

    def test_binary_only_section_has_extended_headers_not_hunks(self) -> None:
        sections = _split_unified_diff_by_file(BINARY_ONLY_DIFF)
        self.assertIn("assets/logo.png", sections)
        section = sections["assets/logo.png"]
        self.assertEqual(section.hunk_text, "")
        self.assertIn(
            "Binary files a/assets/logo.png and b/assets/logo.png differ",
            section.extended_headers,
        )

    def test_mode_only_change_keeps_extended_headers(self) -> None:
        sections = _split_unified_diff_by_file(MODE_ONLY_DIFF)
        section = sections["scripts/deploy.sh"]
        self.assertEqual(section.hunk_text, "")
        self.assertIn("old mode 100644", section.extended_headers)
        self.assertIn("new mode 100755", section.extended_headers)

    def test_empty_file_add_and_delete_keep_extended_headers(self) -> None:
        add_sections = _split_unified_diff_by_file(EMPTY_FILE_ADD_DIFF)
        self.assertIn(
            "new file mode 100644", add_sections["newfile.txt"].extended_headers
        )

        delete_sections = _split_unified_diff_by_file(EMPTY_FILE_DELETE_DIFF)
        self.assertIn(
            "deleted file mode 100644", delete_sections["removed.txt"].extended_headers
        )

    def test_quoted_path_with_space_resolves_to_files_api_filename(self) -> None:
        sections = _split_unified_diff_by_file(QUOTED_SPACE_DIFF)
        self.assertIn("docs/release notes.md", sections)
        self.assertIn("-old", sections["docs/release notes.md"].hunk_text)

    def test_c_escaped_path_decodes_to_literal_characters(self) -> None:
        sections = _split_unified_diff_by_file(ESCAPED_TAB_DIFF)
        self.assertIn("weird\tname.txt", sections)

    def test_quoted_rename_paths_are_keyed_by_both_sides(self) -> None:
        sections = _split_unified_diff_by_file(QUOTED_RENAME_DIFF)
        self.assertIn("old dir/f.txt", sections)
        self.assertIn("new dir/f.txt", sections)
        self.assertIn("rename from", sections["new dir/f.txt"].extended_headers)

    def test_copy_paths_are_keyed_by_both_sides(self) -> None:
        sections = _split_unified_diff_by_file(COPY_DIFF)
        self.assertIn("orig.json", sections)
        self.assertIn("copy.json", sections)
        self.assertIn("copy from", sections["copy.json"].extended_headers)

    def test_dev_null_sides_do_not_affect_added_or_deleted_file_keys(self) -> None:
        added = _split_unified_diff_by_file(ADDED_FILE_WITH_DEV_NULL_DIFF)
        self.assertIn("line one", added["new_module.py"].hunk_text)

        deleted = _split_unified_diff_by_file(DELETED_FILE_WITH_DEV_NULL_DIFF)
        self.assertIn("line one", deleted["old_module.py"].hunk_text)


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

    def test_binary_file_renders_extended_header_instead_of_generic_placeholder(
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
        full_diff_sections = _split_unified_diff_by_file(BINARY_ONLY_DIFF)

        result = _format_pr_diff(files, full_diff_sections=full_diff_sections)

        self.assertNotIn("(Patch unavailable from GitHub for this file.)", result)
        self.assertIn(
            "Binary files a/assets/logo.png and b/assets/logo.png differ", result
        )
        self.assertNotIn("[NEW:", result)

    def test_mode_only_change_renders_extended_header_not_placeholder(self) -> None:
        files = [
            SimpleNamespace(
                filename="scripts/deploy.sh",
                previous_filename=None,
                status="modified",
                patch=None,
            )
        ]
        full_diff_sections = _split_unified_diff_by_file(MODE_ONLY_DIFF)

        result = _format_pr_diff(files, full_diff_sections=full_diff_sections)

        self.assertNotIn("(Patch unavailable from GitHub for this file.)", result)
        self.assertIn("old mode 100644", result)
        self.assertIn("new mode 100755", result)

    def test_no_full_diff_sections_still_falls_back_to_placeholder(self) -> None:
        files = [
            SimpleNamespace(
                filename="unknown.bin",
                previous_filename=None,
                status="modified",
                patch=None,
            )
        ]

        result = _format_pr_diff(files, full_diff_sections={})

        self.assertIn("(Patch unavailable from GitHub for this file.)", result)

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
        full_diff_sections = {
            "README.md": _DiffFileSection(hunk_text="@@ -1,1 +1,1 @@\n-wrong\n+wrong")
        }

        result = _format_pr_diff(files, full_diff_sections=full_diff_sections)

        self.assertIn("direct patch updated", result)
        self.assertNotIn("wrong", result)


class FetchFullPrDiffTest(unittest.TestCase):
    def test_returns_diff_text_on_success(self) -> None:
        requester = _FakeRequester(status=200, output=FULL_DIFF_WITH_LARGE_FILE)
        pr = SimpleNamespace(
            requester=requester,
            url="https://api.github.com/repos/acme/widgets/pulls/321",
        )

        result = _fetch_full_pr_diff(pr)

        self.assertEqual(result, FULL_DIFF_WITH_LARGE_FILE)
        verb, url, headers = requester.calls[0]
        self.assertEqual(verb, "GET")
        self.assertEqual(url, pr.url)
        self.assertEqual(headers, {"Accept": "application/vnd.github.v3.diff"})

    def test_returns_none_on_406_aggregate_diff_too_large(self) -> None:
        requester = _FakeRequester(status=406, output="")
        pr = SimpleNamespace(
            requester=requester,
            url="https://api.github.com/repos/acme/widgets/pulls/321",
        )

        self.assertIsNone(_fetch_full_pr_diff(pr))

    def test_returns_none_on_exception(self) -> None:
        requester = _FakeRequester(raises=True)
        pr = SimpleNamespace(
            requester=requester,
            url="https://api.github.com/repos/acme/widgets/pulls/321",
        )

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

    def _gather(self, *, files, requester):
        pr = self._make_pr(files=files, requester=requester)
        github = MagicMock()
        github.get_pull.return_value = pr

        with (
            patch("workflows.review_pr.resolve_issue_number_for_pr", return_value=None),
            patch("workflows.review_pr.repo_local_skill_path_for_dispatch", return_value=None),
            patch("workflows.review_pr.resolve_spec_context_for_pr_via_api", return_value={}),
        ):
            return gather_review_context(
                github,
                owner="acme",
                repo="widgets",
                pr_number=321,
                trigger_source="pull_request",
                requester="alice",
                workspace_path=Path("/tmp"),
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
        context = self._gather(files=files, requester=requester)

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
        self._gather(files=files, requester=requester)

        # No file needed the fallback, so the extra request is never made.
        self.assertEqual(requester.calls, [])

    def test_binary_file_still_shows_extended_header_when_diff_only_has_binary_marker(
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
        context = self._gather(files=files, requester=requester)

        self.assertIn(
            "Binary files a/assets/logo.png and b/assets/logo.png differ",
            context["pr_diff_text"],
        )
        self.assertEqual(context["diff_line_map"], {})

    def test_mode_only_change_surfaces_permission_bit_in_pr_diff_text(self) -> None:
        files = [
            SimpleNamespace(
                filename="scripts/deploy.sh",
                previous_filename=None,
                status="modified",
                patch=None,
            )
        ]
        requester = _FakeRequester(status=200, output=MODE_ONLY_DIFF)
        context = self._gather(files=files, requester=requester)

        self.assertNotIn(
            "(Patch unavailable from GitHub for this file.)", context["pr_diff_text"]
        )
        self.assertIn("old mode 100644", context["pr_diff_text"])
        self.assertIn("new mode 100755", context["pr_diff_text"])

    def test_quoted_path_with_space_resolves_and_is_annotated(self) -> None:
        files = [
            SimpleNamespace(
                filename="docs/release notes.md",
                previous_filename=None,
                status="modified",
                patch=None,
            )
        ]
        requester = _FakeRequester(status=200, output=QUOTED_SPACE_DIFF)
        context = self._gather(files=files, requester=requester)

        self.assertNotIn(
            "(Patch unavailable from GitHub for this file.)", context["pr_diff_text"]
        )
        self.assertIn("[NEW:1]", context["pr_diff_text"])
        self.assertIn("docs/release notes.md", context["diff_line_map"])


if __name__ == "__main__":
    unittest.main()
