"""Tests for the API-backed repo-local skill helpers.

The webhook hands in a :class:`Github` repository handle (no
filesystem checkout), so the cloud-mode prompt builders use
:func:`repo_local_skill_path_for_dispatch` to resolve the consuming
repository's ``.agents/skills/<name>-local/SKILL.md`` companion via
the GitHub API. Returning a repo-relative path string (rather than a
filesystem :class:`pathcore.Path`) lets the cloud agent read the file
through its inherited working directory.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from . import conftest  # noqa: F401

from oz.repo_local import (
    format_repo_local_prompt_section,
    repo_local_skill_path_for_dispatch,
)


class RepoLocalSkillPathForDispatchTest(unittest.TestCase):
    def test_returns_repo_relative_path_when_skill_has_body(self) -> None:
        repo_handle = MagicMock()
        contents = MagicMock()
        contents.decoded_content = (
            b"---\n"
            b"name: triage-issue-local\n"
            b"---\n"
            b"\n"
            b"# Repo overrides\n"
            b"\n"
            b"- Apply project-specific triage rules.\n"
        )
        repo_handle.get_contents.return_value = contents
        self.assertEqual(
            repo_local_skill_path_for_dispatch(repo_handle, "triage-issue"),
            ".agents/skills/triage-issue-local/SKILL.md",
        )
        repo_handle.get_contents.assert_called_once_with(
            ".agents/skills/triage-issue-local/SKILL.md"
        )

    def test_returns_none_when_skill_file_missing(self) -> None:
        from github.GithubException import UnknownObjectException

        repo_handle = MagicMock()
        repo_handle.get_contents.side_effect = UnknownObjectException(
            404, {"message": "Not Found"}, {}
        )
        self.assertIsNone(
            repo_local_skill_path_for_dispatch(repo_handle, "review-pr")
        )

    def test_returns_none_when_skill_body_is_only_frontmatter(self) -> None:
        # A companion file that only contains YAML frontmatter is
        # treated as absent so the prompt section is omitted.
        repo_handle = MagicMock()
        contents = MagicMock()
        contents.decoded_content = b"---\nname: review-pr-local\n---\n\n"
        repo_handle.get_contents.return_value = contents
        self.assertIsNone(
            repo_local_skill_path_for_dispatch(repo_handle, "review-pr")
        )

    def test_returns_none_when_skill_body_is_blank(self) -> None:
        repo_handle = MagicMock()
        contents = MagicMock()
        contents.decoded_content = b"   \n\t\n"
        repo_handle.get_contents.return_value = contents
        self.assertIsNone(
            repo_local_skill_path_for_dispatch(repo_handle, "review-pr")
        )

    def test_returns_none_for_blank_skill_name(self) -> None:
        repo_handle = MagicMock()
        self.assertIsNone(repo_local_skill_path_for_dispatch(repo_handle, ""))
        self.assertIsNone(repo_local_skill_path_for_dispatch(repo_handle, "   "))
        repo_handle.get_contents.assert_not_called()


class FormatRepoLocalPromptSectionTest(unittest.TestCase):
    def test_renders_section_with_string_path(self) -> None:
        # The cloud-mode helper returns a repo-relative string path.
        # The prompt formatter must accept it without coercion.
        section = format_repo_local_prompt_section(
            "review-pr", ".agents/skills/review-pr-local/SKILL.md"
        )
        self.assertIn("Repository-specific guidance for `review-pr`", section)
        self.assertIn(".agents/skills/review-pr-local/SKILL.md", section)

    def test_renders_section_with_path_object(self) -> None:
        # Workspace-backed callers can hand in an absolute
        # :class:`pathcore.Path`; the formatter must also accept it so
        # both path-resolution modes share the same helper.
        from pathlib import Path

        section = format_repo_local_prompt_section(
            "triage-issue",
            Path("/workspace/oz-for-oss/.agents/skills/triage-issue-local/SKILL.md"),
        )
        self.assertIn(
            "/workspace/oz-for-oss/.agents/skills/triage-issue-local/SKILL.md",
            section,
        )


if __name__ == "__main__":
    unittest.main()
