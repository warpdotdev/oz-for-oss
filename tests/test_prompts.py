from __future__ import annotations

import unittest

from . import conftest  # noqa: F401

from workflows.respond_to_pr_comment import build_pr_comment_prompt
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


if __name__ == "__main__":
    unittest.main()
