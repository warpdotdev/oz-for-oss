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
from workflows.create_implementation_from_issue import build_create_implementation_prompt
from workflows.review_pr import build_review_prompt_for_dispatch
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

class RepoScopedVerificationPromptTest(unittest.TestCase):
    def test_review_prompt_requires_verifying_pr_head_repo_and_ref(self) -> None:
        prompt = build_review_prompt_for_dispatch(
            {
                "owner": "warpdotdev",
                "repo": "warp",
                "pr_number": 88,
                "pr_title": "feat: update review flow",
                "pr_body": "body",
                "base_branch": "main",
                "head_branch": "feature/review-flow",
                "head_repo_full_name": "warpdotdev/warp",
                "head_sha": "abc123",
                "trigger_source": "pull_request",
                "focus_line": "Perform a general review.",
                "issue_line": "No associated issue resolved for spec lookup.",
                "skill_name": "review-pr",
                "supplemental_skill_line": "Also apply security-review-pr.",
                "repo_local_section": "",
                "non_member_review_section": "",
                "pr_description_text": "description",
                "pr_diff_text": "diff",
                "spec_context_text": "",
            }
        )

        self.assertIn("Repository-Scoped Verification", prompt)
        self.assertIn("Target repository: `warpdotdev/warp`", prompt)
        self.assertIn("Target ref/branch: `feature/review-flow`", prompt)
        self.assertIn("Target commit SHA: `abc123`", prompt)
        self.assertIn("the top-level `body` field of `review.json`", prompt)
        self.assertNotIn("Do not run `git fetch`, `git checkout`", prompt)
        self.assertNotIn("webhook", prompt.lower())

    def test_implementation_prompt_uses_target_repository_without_hardcoding(self) -> None:
        prompt = build_create_implementation_prompt(
            owner="acme",
            repo="widgets",
            issue_number=439,
            issue_title="Run repo-scoped verification",
            issue_labels=["enhancement"],
            issue_assignees=["oz-agent"],
            spec_context_text="No approved or repository spec context was found.",
            target_branch="oz-agent/implement-issue-439",
            default_branch="main",
            implement_specs_skill_path=".agents/skills/implement-specs/SKILL.md",
            spec_driven_implementation_skill_path=".agents/skills/spec-driven-implementation/SKILL.md",
            implement_issue_skill_path=".agents/skills/implement-issue/SKILL.md",
            coauthor_directives="",
        )

        self.assertIn("Target repository: `acme/widgets`", prompt)
        self.assertIn("Target ref/branch: `oz-agent/implement-issue-439`", prompt)
        self.assertIn("do not hard-code behavior for any one repository name", prompt)
        self.assertIn("commands attempted", prompt)
        self.assertNotIn("webhook", prompt.lower())

    def test_pr_comment_prompt_verifies_agent_push_repository(self) -> None:
        prompt = build_pr_comment_prompt(
            {
                "owner": "octo",
                "repo": "tools",
                "pr_number": 12,
                "head_branch": "feature",
                "head_repo_full_name": "octo/tools",
                "base_branch": "main",
                "base_repo_full_name": "octo/tools",
                "pr_title": "feat: add tool",
                "requester": "alice",
                "trigger_kind": "conversation",
                "trigger_comment_id": 99,
                "spec_context_text": "No spec context.",
                "coauthor_directives": "",
                "branch_strategy": "push-head",
                "agent_push_repo_full_name": "octo/tools",
                "agent_push_branch": "feature",
            }
        )

        self.assertIn("Target repository: `octo/tools`", prompt)
        self.assertIn("Target ref/branch: `feature`", prompt)
        self.assertIn("pass/fail/skipped status", prompt)
        self.assertNotIn("webhook", prompt.lower())


class PrCommentContextBranchSafetyTest(unittest.TestCase):
    def _pr(
        self,
        *,
        head_repo: str,
        base_repo: str,
        maintainer_can_modify: bool = False,
    ) -> SimpleNamespace:
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
            maintainer_can_modify=maintainer_can_modify,
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
                event={
                    "comment": {
                        "author_association": "MEMBER",
                        "user": {"login": "alice"},
                    }
                },
                pr=pr,
            )

        self.assertFalse(context["is_cross_repository"])
        self.assertTrue(context["head_branch_exists_in_base"])
        self.assertTrue(context["can_push_to_head_branch"])
        self.assertEqual(context["branch_strategy"], "push-head")
        self.assertEqual(context["agent_push_repo_full_name"], "acme/widgets")
        self.assertEqual(context["agent_push_branch"], "feature")

    def test_context_uses_fallback_branch_for_fork_without_maintainer_modify(self) -> None:
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
                event={
                    "comment": {
                        "author_association": "MEMBER",
                        "user": {"login": "alice"},
                    }
                },
                pr=pr,
            )

        self.assertTrue(context["is_cross_repository"])
        self.assertTrue(context["head_branch_exists_in_base"])
        self.assertFalse(context["can_push_to_head_branch"])
        self.assertEqual(context["branch_strategy"], "fallback-pr-to-fork")
        self.assertEqual(context["agent_push_repo_full_name"], "acme/widgets")
        self.assertEqual(context["agent_push_branch"], "oz-agent/respond-pr-12")
        self.assertEqual(context["fallback_pr_base_repo_full_name"], "contributor/widgets")
        self.assertEqual(context["fallback_pr_base_branch"], "feature")
        self.assertEqual(context["fallback_pr_head"], "acme:oz-agent/respond-pr-12")
        self.assertTrue(context["trigger_actor_is_trusted"])

    def test_context_pushes_to_fork_head_when_maintainers_can_modify(self) -> None:
        github = MagicMock()
        github.get_git_ref.return_value = object()
        pr = self._pr(
            head_repo="contributor/widgets",
            base_repo="acme/widgets",
            maintainer_can_modify=True,
        )

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
                event={
                    "comment": {
                        "author_association": "MEMBER",
                        "user": {"login": "alice"},
                    }
                },
                pr=pr,
            )

        self.assertTrue(context["is_cross_repository"])
        self.assertTrue(context["maintainer_can_modify"])
        self.assertTrue(context["can_push_to_head_branch"])
        self.assertEqual(context["branch_strategy"], "push-head")
        self.assertEqual(context["agent_push_repo_full_name"], "contributor/widgets")
        self.assertEqual(context["agent_push_branch"], "feature")

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
        self.assertEqual(context["branch_strategy"], "blocked")


class PrCommentPromptBranchStrategyTest(unittest.TestCase):
    def _context(self) -> dict[str, object]:
        return {
            "owner": "acme",
            "repo": "widgets",
            "pr_number": 12,
            "head_branch": "feature",
            "head_repo_full_name": "acme/widgets",
            "base_branch": "main",
            "base_repo_full_name": "acme/widgets",
            "pr_title": "feat: add widget",
            "requester": "alice",
            "trigger_kind": "conversation",
            "trigger_comment_id": 99,
            "spec_context_text": "No spec context.",
            "coauthor_directives": "",
            "branch_strategy": "push-head",
            "agent_push_repo_full_name": "acme/widgets",
            "agent_push_branch": "feature",
        }

    def test_direct_fork_prompt_targets_fork_head_branch(self) -> None:
        context = self._context()
        context.update(
            {
                "head_repo_full_name": "contributor/widgets",
                "base_repo_full_name": "acme/widgets",
                "agent_push_repo_full_name": "contributor/widgets",
            }
        )

        prompt = build_pr_comment_prompt(context)

        self.assertIn("maintainers are allowed to modify the fork head branch", prompt)
        self.assertIn("push to `contributor/widgets:feature`", prompt)
        self.assertIn("Do not push a same-named branch to `acme/widgets`", prompt)

    def test_fallback_prompt_requires_metadata_for_follow_up_pr(self) -> None:
        context = self._context()
        context.update(
            {
                "head_repo_full_name": "contributor/widgets",
                "base_repo_full_name": "acme/widgets",
                "branch_strategy": "fallback-pr-to-fork",
                "agent_push_repo_full_name": "acme/widgets",
                "agent_push_branch": "oz-agent/respond-pr-12",
                "fallback_pr_base_repo_full_name": "contributor/widgets",
                "fallback_pr_base_branch": "feature",
                "fallback_pr_head": "acme:oz-agent/respond-pr-12",
            }
        )

        prompt = build_pr_comment_prompt(context)

        self.assertIn("maintainers cannot modify the fork head branch", prompt)
        self.assertIn("Do not push to `contributor/widgets:feature`", prompt)
        self.assertIn("Create or reuse branch `oz-agent/respond-pr-12`", prompt)
        self.assertIn(
            "fetch `contributor/widgets:feature` and make sure `oz-agent/respond-pr-12` starts from that fork head commit",
            prompt,
        )
        self.assertIn("follow-up PR from `acme:oz-agent/respond-pr-12`", prompt)
        self.assertIn("use `oz-agent/respond-pr-12` exactly", prompt)


if __name__ == "__main__":
    unittest.main()
