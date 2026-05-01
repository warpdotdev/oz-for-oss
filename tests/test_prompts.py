from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from github.GithubException import UnknownObjectException

from . import conftest  # noqa: F401

from workflows.respond_to_pr_comment import (
    build_pr_comment_prompt,
    gather_pr_comment_context,
)
from workflows.verify_pr_comment import build_verification_prompt


class FetchContextCommandPromptTest(unittest.TestCase):
    def test_respond_prompt_uses_global_repo_arg_before_subcommand(self) -> None:
        prompt = build_pr_comment_prompt(
            {
                "owner": "acme",
                "repo": "widgets",
                "pr_number": 12,
                "head_branch": "feature",
                "base_branch": "main",
                "pr_title": "feat: add widget",
                "requester": "alice",
                "trigger_kind": "conversation",
                "trigger_comment_id": 99,
                "spec_context_text": "No spec context.",
                "coauthor_directives": "",
            }
        )
        self.assertIn(
            "python .agents/skills/implement-specs/scripts/fetch_github_context.py "
            "--repo acme/widgets pr --number 12",
            prompt,
        )
        self.assertIn(
            "python .agents/skills/implement-specs/scripts/fetch_github_context.py "
            "--repo acme/widgets pr-diff --number 12",
            prompt,
        )
        self.assertNotIn("fetch_github_context.py pr --repo", prompt)

    def test_verify_prompt_uses_global_repo_arg_before_subcommand(self) -> None:
        prompt = build_verification_prompt(
            owner="acme",
            repo="widgets",
            pr_number=12,
            base_branch="main",
            head_branch="feature",
            trigger_comment_id=99,
            requester="alice",
            verification_skills_text="- verify-ui",
        )
        self.assertIn(
            "python .agents/skills/implement-specs/scripts/fetch_github_context.py "
            "--repo acme/widgets pr --number 12",
            prompt,
        )
        self.assertIn(
            "python .agents/skills/implement-specs/scripts/fetch_github_context.py "
            "--repo acme/widgets pr-diff --number 12",
            prompt,
        )
        self.assertNotIn("fetch_github_context.py pr --repo", prompt)


class PrCommentContextBranchSafetyTest(unittest.TestCase):
    def _pr(self, *, head_repo: str, base_repo: str) -> SimpleNamespace:
        return SimpleNamespace(
            head=SimpleNamespace(
                ref="feature",
                repo=SimpleNamespace(full_name=head_repo),
            ),
            base=SimpleNamespace(
                ref="main",
                repo=SimpleNamespace(full_name=base_repo),
            ),
            title="feat: add widget",
        )

    def test_context_allows_push_for_existing_same_repo_branch(self) -> None:
        github = MagicMock()
        github.get_git_ref.return_value = object()
        pr = self._pr(head_repo="acme/widgets", base_repo="acme/widgets")

        with patch(
            "workflows.respond_to_pr_comment.resolve_spec_context_for_pr_via_api",
            return_value={"spec_entries": []},
        ):
            context = gather_pr_comment_context(
                github,
                owner="acme",
                repo="widgets",
                pr_number=12,
                trigger_kind="conversation",
                trigger_comment_id=99,
                requester="alice",
                event={},
                pr=pr,
            )

        self.assertFalse(context["is_cross_repository"])
        self.assertTrue(context["head_branch_exists_in_base"])
        self.assertTrue(context["can_push_to_head_branch"])

    def test_context_blocks_push_for_fork_pr_even_when_branch_name_matches(self) -> None:
        github = MagicMock()
        github.get_git_ref.return_value = object()
        pr = self._pr(head_repo="contributor/widgets", base_repo="acme/widgets")

        with patch(
            "workflows.respond_to_pr_comment.resolve_spec_context_for_pr_via_api",
            return_value={"spec_entries": []},
        ):
            context = gather_pr_comment_context(
                github,
                owner="acme",
                repo="widgets",
                pr_number=12,
                trigger_kind="conversation",
                trigger_comment_id=99,
                requester="alice",
                event={},
                pr=pr,
            )

        self.assertTrue(context["is_cross_repository"])
        self.assertTrue(context["head_branch_exists_in_base"])
        self.assertFalse(context["can_push_to_head_branch"])

    def test_context_blocks_push_when_branch_would_be_created(self) -> None:
        github = MagicMock()
        github.get_git_ref.side_effect = UnknownObjectException(404, {}, {})
        pr = self._pr(head_repo="acme/widgets", base_repo="acme/widgets")

        with patch(
            "workflows.respond_to_pr_comment.resolve_spec_context_for_pr_via_api",
            return_value={"spec_entries": []},
        ):
            context = gather_pr_comment_context(
                github,
                owner="acme",
                repo="widgets",
                pr_number=12,
                trigger_kind="conversation",
                trigger_comment_id=99,
                requester="alice",
                event={},
                pr=pr,
            )

        self.assertFalse(context["is_cross_repository"])
        self.assertFalse(context["head_branch_exists_in_base"])
        self.assertFalse(context["can_push_to_head_branch"])


if __name__ == "__main__":
    unittest.main()
