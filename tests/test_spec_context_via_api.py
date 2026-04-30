"""Tests for the API-backed spec-context helpers.

The Vercel webhook does not check out the consuming repository, so
cloud-mode callers resolve spec context entirely through the GitHub
API: approved spec PRs are looked up via ``find_matching_spec_prs``,
and ``specs/GH<N>/{product,tech}.md`` files are read via
:func:`read_repo_spec_files` instead of walking a workspace
directory.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from . import conftest  # noqa: F401

from oz.helpers import (
    read_repo_spec_files,
    resolve_spec_context_for_issue_via_api,
    resolve_spec_context_for_pr_via_api,
)


def _content_file(text: bytes) -> Any:
    cf = MagicMock()
    cf.decoded_content = text
    return cf


class ReadRepoSpecFilesTest(unittest.TestCase):
    def test_returns_decoded_product_and_tech_when_both_present(self) -> None:
        repo_handle = MagicMock()

        def _get_contents(path: str, ref: str | None = None) -> Any:
            mapping = {
                "specs/GH91/product.md": _content_file(
                    b"# Product spec\n\nReporter goal."
                ),
                "specs/GH91/tech.md": _content_file(
                    b"# Tech spec\n\nDesign details."
                ),
            }
            if path in mapping:
                return mapping[path]
            from github.GithubException import UnknownObjectException

            raise UnknownObjectException(404, {"message": "Not Found"}, {})

        repo_handle.get_contents.side_effect = _get_contents
        entries = read_repo_spec_files(repo_handle, 91)
        self.assertEqual(
            entries,
            [
                ("specs/GH91/product.md", "# Product spec\n\nReporter goal."),
                ("specs/GH91/tech.md", "# Tech spec\n\nDesign details."),
            ],
        )

    def test_omits_missing_files_silently(self) -> None:
        # When only ``tech.md`` exists the helper returns just the tech
        # entry rather than aborting the whole resolver.
        repo_handle = MagicMock()
        from github.GithubException import UnknownObjectException

        def _get_contents(path: str, ref: str | None = None) -> Any:
            if path == "specs/GH7/tech.md":
                return _content_file(b"tech body")
            raise UnknownObjectException(404, {"message": "Not Found"}, {})

        repo_handle.get_contents.side_effect = _get_contents
        entries = read_repo_spec_files(repo_handle, 7)
        self.assertEqual(entries, [("specs/GH7/tech.md", "tech body")])

    def test_returns_empty_when_neither_file_exists(self) -> None:
        from github.GithubException import UnknownObjectException

        repo_handle = MagicMock()
        repo_handle.get_contents.side_effect = UnknownObjectException(
            404, {"message": "Not Found"}, {}
        )
        self.assertEqual(read_repo_spec_files(repo_handle, 42), [])


class ResolveSpecContextForIssueViaApiTest(unittest.TestCase):
    def test_picks_approved_spec_pr_when_available(self) -> None:
        from oz import helpers as helpers_mod

        # Replace ``find_matching_spec_prs`` with a stub so we can
        # control the approved/unapproved tuple without touching the
        # PyGithub surface.
        repo_handle = MagicMock()
        repo_handle.get_contents.return_value = _content_file(b"approved body")

        approved = [
            {
                "number": 123,
                "url": "https://example.test/pr/123",
                "head_ref_name": "oz-agent/spec-issue-91",
                "head_repo_full_name": "acme/widgets",
                "spec_files": ["specs/GH91/product.md"],
            }
        ]
        unapproved: list[dict[str, Any]] = []

        original = helpers_mod.find_matching_spec_prs
        helpers_mod.find_matching_spec_prs = MagicMock(  # type: ignore[assignment]
            return_value=(approved, unapproved)
        )
        try:
            result = resolve_spec_context_for_issue_via_api(
                repo_handle, "acme", "widgets", 91
            )
        finally:
            helpers_mod.find_matching_spec_prs = original  # type: ignore[assignment]

        self.assertEqual(result["spec_context_source"], "approved-pr")
        self.assertEqual(result["selected_spec_pr"], approved[0])
        self.assertEqual(
            result["spec_entries"],
            [{"path": "specs/GH91/product.md", "content": "approved body"}],
        )

    def test_falls_back_to_directory_specs_when_no_approved_pr(self) -> None:
        from oz import helpers as helpers_mod
        from github.GithubException import UnknownObjectException

        repo_handle = MagicMock()

        def _get_contents(path: str, ref: str | None = None) -> Any:
            if path == "specs/GH91/product.md":
                return _content_file(b"product spec body")
            if path == "specs/GH91/tech.md":
                return _content_file(b"tech spec body")
            raise UnknownObjectException(404, {"message": "Not Found"}, {})

        repo_handle.get_contents.side_effect = _get_contents

        original = helpers_mod.find_matching_spec_prs
        helpers_mod.find_matching_spec_prs = MagicMock(  # type: ignore[assignment]
            return_value=([], [])
        )
        try:
            result = resolve_spec_context_for_issue_via_api(
                repo_handle, "acme", "widgets", 91
            )
        finally:
            helpers_mod.find_matching_spec_prs = original  # type: ignore[assignment]

        self.assertEqual(result["spec_context_source"], "directory")
        self.assertIsNone(result["selected_spec_pr"])
        self.assertEqual(
            result["spec_entries"],
            [
                {"path": "specs/GH91/product.md", "content": "product spec body"},
                {"path": "specs/GH91/tech.md", "content": "tech spec body"},
            ],
        )

    def test_returns_empty_source_when_no_spec_context(self) -> None:
        from oz import helpers as helpers_mod
        from github.GithubException import UnknownObjectException

        repo_handle = MagicMock()
        repo_handle.get_contents.side_effect = UnknownObjectException(
            404, {"message": "Not Found"}, {}
        )

        original = helpers_mod.find_matching_spec_prs
        helpers_mod.find_matching_spec_prs = MagicMock(  # type: ignore[assignment]
            return_value=([], [])
        )
        try:
            result = resolve_spec_context_for_issue_via_api(
                repo_handle, "acme", "widgets", 91
            )
        finally:
            helpers_mod.find_matching_spec_prs = original  # type: ignore[assignment]

        self.assertEqual(result["spec_context_source"], "")
        self.assertEqual(result["spec_entries"], [])
        self.assertIsNone(result["selected_spec_pr"])

    def test_raises_when_approved_pr_lives_on_a_fork(self) -> None:
        # Spec PRs from forks cannot be pushed to via the bot's App
        # token, so ``resolve_spec_context_for_issue_via_api`` raises\
        # to surface the misconfiguration loudly.
        from oz import helpers as helpers_mod

        repo_handle = MagicMock()
        approved = [
            {
                "number": 99,
                "url": "https://example.test/pr/99",
                "head_ref_name": "fork-branch",
                "head_repo_full_name": "fork-owner/widgets",
                "spec_files": ["specs/GH99/product.md"],
            }
        ]
        original = helpers_mod.find_matching_spec_prs
        helpers_mod.find_matching_spec_prs = MagicMock(  # type: ignore[assignment]
            return_value=(approved, [])
        )
        try:
            with self.assertRaises(RuntimeError):
                resolve_spec_context_for_issue_via_api(
                    repo_handle, "acme", "widgets", 99
                )
        finally:
            helpers_mod.find_matching_spec_prs = original  # type: ignore[assignment]


class ResolveSpecContextForPrViaApiTest(unittest.TestCase):
    def test_returns_empty_when_pr_has_no_linked_issue(self) -> None:
        from oz import helpers as helpers_mod

        repo_handle = MagicMock()
        pr = MagicMock()
        pr.get_files.return_value = []

        original = helpers_mod.resolve_issue_number_for_pr
        helpers_mod.resolve_issue_number_for_pr = MagicMock(  # type: ignore[assignment]
            return_value=None
        )
        try:
            result = resolve_spec_context_for_pr_via_api(
                repo_handle, "acme", "widgets", pr
            )
        finally:
            helpers_mod.resolve_issue_number_for_pr = original  # type: ignore[assignment]

        self.assertIsNone(result["issue_number"])
        self.assertEqual(result["spec_entries"], [])
        self.assertEqual(result["spec_context_source"], "")

    def test_passes_resolved_issue_number_to_api_resolver(self) -> None:
        from oz import helpers as helpers_mod

        repo_handle = MagicMock()
        pr = MagicMock()
        pr.get_files.return_value = [
            SimpleNamespace(filename="specs/GH7/product.md"),
        ]

        sentinel_context = {
            "selected_spec_pr": None,
            "approved_spec_prs": [],
            "unapproved_spec_prs": [],
            "spec_context_source": "directory",
            "spec_entries": [{"path": "specs/GH7/product.md", "content": "x"}],
        }

        original_resolve = helpers_mod.resolve_issue_number_for_pr
        original_via_api = helpers_mod.resolve_spec_context_for_issue_via_api
        helpers_mod.resolve_issue_number_for_pr = MagicMock(  # type: ignore[assignment]
            return_value=7
        )
        helpers_mod.resolve_spec_context_for_issue_via_api = MagicMock(  # type: ignore[assignment]
            return_value=dict(sentinel_context)
        )
        try:
            result = resolve_spec_context_for_pr_via_api(
                repo_handle, "acme", "widgets", pr
            )
        finally:
            helpers_mod.resolve_issue_number_for_pr = original_resolve  # type: ignore[assignment]
            helpers_mod.resolve_spec_context_for_issue_via_api = original_via_api  # type: ignore[assignment]

        self.assertEqual(result["issue_number"], 7)
        self.assertEqual(result["spec_context_source"], "directory")
        self.assertEqual(
            result["spec_entries"],
            [{"path": "specs/GH7/product.md", "content": "x"}],
        )


if __name__ == "__main__":
    unittest.main()
