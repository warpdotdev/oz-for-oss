"""Tests for ``control_plane.lib.builders``.

The builders are thin wrappers around the ``gather_*_context`` /
``build_*_prompt`` helpers exposed by the GitHub Actions entrypoints.
The mirrored copies live under ``control-plane/lib/scripts/`` after
``scripts/vercel_install.sh`` runs. The tests stub each gather/build
helper so the assertions stay focused on builder wiring (payload
parsing, repo handle resolution, DispatchRequest shape).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from . import conftest  # noqa: F401


def _ensure_module(name: str) -> ModuleType:
    """Return a stub module under *name* in ``sys.modules``.

    Replaces any previous instance so each test class starts with a
    clean stub. Nested modules (``a.b``) require the parent module to
    exist; the helper installs missing parents as bare ``ModuleType``
    instances so attribute lookups work.
    """
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        sub = ".".join(parts[: i])
        if sub not in sys.modules:
            sys.modules[sub] = ModuleType(sub)
    module = ModuleType(name)
    sys.modules[name] = module
    return module


class _BuilderTestBase(unittest.TestCase):
    """Mixin that owns the stub modules the builders import lazily."""

    def setUp(self) -> None:
        super().setUp()
        self._module_keys = [
            "scripts",
            "scripts.review_pr",
            "scripts.respond_to_pr_comment",
            "scripts.verify_pr_comment",
            "scripts.enforce_pr_issue_state",
            "oz_workflows",
            "oz_workflows.helpers",
        ]
        self._original_modules = {
            key: sys.modules.get(key) for key in self._module_keys
        }
        # The builders import :class:`WorkflowProgressComment` and the
        # workflow-specific ``format_*_start_line`` helpers lazily to
        # avoid pulling PyGithub into the test path. Stub the helper
        # module so each test can drive the lifecycle without going
        # through the production helper.
        oz = _ensure_module("oz_workflows")
        helpers = _ensure_module("oz_workflows.helpers")
        oz.helpers = helpers  # type: ignore[attr-defined]
        self.progress_instances: list[MagicMock] = []

        def _progress_factory(*args: Any, **kwargs: Any) -> MagicMock:
            instance = MagicMock(
                comment_id=4242,
                run_id="run-uuid-hex",
                start=MagicMock(),
            )
            self.progress_instances.append(instance)
            return instance

        helpers.WorkflowProgressComment = MagicMock(  # type: ignore[attr-defined]
            side_effect=_progress_factory
        )
        helpers.format_review_start_line = MagicMock(  # type: ignore[attr-defined]
            return_value="I'm starting a first review of this pull request."
        )

    def tearDown(self) -> None:
        for key, value in self._original_modules.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        super().tearDown()


class BuildReviewRequestTest(_BuilderTestBase):
    def setUp(self) -> None:
        super().setUp()
        scripts = _ensure_module("scripts")
        review_module = _ensure_module("scripts.review_pr")
        scripts.review_pr = review_module  # type: ignore[attr-defined]
        review_module.gather_review_context = MagicMock(  # type: ignore[attr-defined]
            return_value={
                "owner": "acme",
                "repo": "widgets",
                "pr_number": 42,
                "pr_title": "feat: add retry",
                "pr_body": "body",
                "base_branch": "main",
                "head_branch": "oz-agent/feature",
                "trigger_source": "pull_request",
                "requester": "alice",
                "focus_line": "Perform a general review.",
                "issue_line": "#100",
                "skill_name": "review-pr",
                "supplemental_skill_line": "Also apply security-review-pr.",
                "repo_local_section": "",
                "non_member_review_section": "",
                "pr_description_text": "PR description body",
                "pr_diff_text": "diff body",
                "spec_context_text": "",
                "diff_line_map": {},
                "diff_content_map": {},
                "is_non_member": False,
                "spec_only": False,
                "pr_author_login": "carol",
                "stakeholder_logins": [],
                "progress_comment_id": 0,
            }
        )
        review_module.build_review_prompt_for_dispatch = MagicMock(  # type: ignore[attr-defined]
            return_value="REVIEW_PROMPT_BODY"
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 1234},
            "pull_request": {"number": 42},
            "sender": {"login": "alice"},
        }

    def test_returns_dispatch_request_with_inlined_prompt(self) -> None:
        from lib.builders import build_review_request
        from lib.routing import WORKFLOW_REVIEW_PR

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")

        request = build_review_request(
            self._payload(),
            github_client=github_client,
            workspace_path=Path("/tmp/ws"),
        )

        self.assertEqual(request.workflow, WORKFLOW_REVIEW_PR)
        self.assertEqual(request.repo, "acme/widgets")
        self.assertEqual(request.installation_id, 1234)
        self.assertEqual(request.title, "PR review #42")
        self.assertEqual(request.skill_name, "review-pr")
        self.assertEqual(request.prompt, "REVIEW_PROMPT_BODY")
        self.assertEqual(request.payload_subset["pr_number"], 42)
        self.assertIn("pr_diff_text", request.payload_subset)
        github_client.get_repo.assert_called_once_with("acme/widgets")
        # The builder must drive the WorkflowProgressComment lifecycle
        # so the cron poller can reconstruct the same comment.
        self.assertEqual(len(self.progress_instances), 1)
        self.progress_instances[0].start.assert_called_once()
        self.assertEqual(request.payload_subset["progress_comment_id"], 4242)
        self.assertEqual(request.payload_subset["progress_run_id"], "run-uuid-hex")

    def test_raises_when_payload_missing_installation_id(self) -> None:
        from lib.builders import build_review_request

        payload = self._payload()
        payload.pop("installation")
        with self.assertRaises(ValueError):
            build_review_request(
                payload,
                github_client=MagicMock(),
                workspace_path=Path("/tmp/ws"),
            )


class BuildRespondRequestTest(_BuilderTestBase):
    def setUp(self) -> None:
        super().setUp()
        scripts = _ensure_module("scripts")
        respond_module = _ensure_module("scripts.respond_to_pr_comment")
        scripts.respond_to_pr_comment = respond_module  # type: ignore[attr-defined]
        respond_module.gather_pr_comment_context = MagicMock(  # type: ignore[attr-defined]
            return_value={
                "owner": "acme",
                "repo": "widgets",
                "pr_number": 7,
                "head_branch": "oz-agent/feature",
                "base_branch": "main",
                "pr_title": "feat: add",
                "requester": "alice",
                "trigger_kind": "review",
                "trigger_comment_id": 999,
                "review_reply_target_id": 999,
                "has_spec_context": False,
                "spec_context_text": "No spec context.",
                "coauthor_line": "",
                "coauthor_directives": "- foo",
                "progress_start_line": "I'm starting",
            }
        )
        respond_module.build_pr_comment_prompt = MagicMock(  # type: ignore[attr-defined]
            return_value="RESPOND_PROMPT_BODY"
        )

    def test_returns_dispatch_request_for_review_comment(self) -> None:
        from lib.builders import build_respond_request
        from lib.routing import WORKFLOW_RESPOND_TO_PR_COMMENT

        github_client = MagicMock()
        repo = MagicMock(name="repo")
        github_client.get_repo.return_value = repo
        pr = MagicMock(name="pr")
        repo.get_pull.return_value = pr

        payload = {
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 1},
            "pull_request": {"number": 7},
            "comment": {"id": 999, "user": {"login": "alice"}},
        }

        request = build_respond_request(
            payload,
            github_client=github_client,
            workspace_path=Path("/tmp/ws"),
        )
        self.assertEqual(request.workflow, WORKFLOW_RESPOND_TO_PR_COMMENT)
        self.assertEqual(request.skill_name, "implement-issue")
        self.assertEqual(request.prompt, "RESPOND_PROMPT_BODY")
        self.assertEqual(request.payload_subset["trigger_comment_id"], 999)
        # The builder consumed the existing PR handle to gather context.
        repo.get_pull.assert_called_once_with(7)
        # Progress lifecycle must be driven before dispatch.
        self.assertEqual(len(self.progress_instances), 1)
        self.progress_instances[0].start.assert_called_once_with("I'm starting")
        self.assertEqual(request.payload_subset["progress_comment_id"], 4242)
        self.assertEqual(request.payload_subset["progress_run_id"], "run-uuid-hex")


class BuildVerifyRequestTest(_BuilderTestBase):
    def setUp(self) -> None:
        super().setUp()
        scripts = _ensure_module("scripts")
        verify_module = _ensure_module("scripts.verify_pr_comment")
        scripts.verify_pr_comment = verify_module  # type: ignore[attr-defined]
        verify_module.gather_verify_context = MagicMock(  # type: ignore[attr-defined]
            return_value={
                "owner": "acme",
                "repo": "widgets",
                "pr_number": 11,
                "base_branch": "main",
                "head_branch": "feature/verify",
                "trigger_comment_id": 555,
                "requester": "alice",
                "verification_skills_text": "- verify-ui at .agents/skills/verify-ui/SKILL.md",
            }
        )
        verify_module.build_verification_prompt = MagicMock(  # type: ignore[attr-defined]
            return_value="VERIFY_PROMPT_BODY"
        )

    def test_returns_dispatch_request_with_verify_prompt(self) -> None:
        from lib.builders import build_verify_request
        from lib.routing import WORKFLOW_VERIFY_PR_COMMENT

        github_client = MagicMock()
        repo = MagicMock(name="repo")
        github_client.get_repo.return_value = repo

        payload = {
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 5},
            "issue": {"number": 11, "pull_request": {}},
            "comment": {"id": 555, "user": {"login": "alice"}, "body": "/oz-verify"},
        }

        request = build_verify_request(
            payload,
            github_client=github_client,
            workspace_path=Path("/tmp/ws"),
        )
        self.assertEqual(request.workflow, WORKFLOW_VERIFY_PR_COMMENT)
        self.assertEqual(request.skill_name, "verify-pr")
        self.assertEqual(request.prompt, "VERIFY_PROMPT_BODY")
        self.assertEqual(request.payload_subset["pr_number"], 11)
        self.assertEqual(len(self.progress_instances), 1)
        self.progress_instances[0].start.assert_called_once()
        self.assertEqual(request.payload_subset["progress_comment_id"], 4242)
        self.assertEqual(request.payload_subset["progress_run_id"], "run-uuid-hex")


class BuildEnforceRequestTest(_BuilderTestBase):
    def setUp(self) -> None:
        super().setUp()
        scripts = _ensure_module("scripts")
        enforce_module = _ensure_module("scripts.enforce_pr_issue_state")
        scripts.enforce_pr_issue_state = enforce_module  # type: ignore[attr-defined]

        # ``EnforceContext`` is a TypedDict; stub it as plain ``dict``.
        enforce_module.EnforceContext = dict  # type: ignore[attr-defined]

        decision = SimpleNamespace(
            action="need-cloud-match",
            allow_review=False,
            reason="need-cloud-match",
            close_comment="",
            context={
                "owner": "acme",
                "repo": "widgets",
                "pr_number": 21,
                "requester": "alice",
                "change_kind": "implementation",
                "required_label": "ready-to-implement",
                "contribution_docs_url": "https://example.test/docs",
            },
        )
        enforce_module.enforce_pr_state_synchronously = MagicMock(  # type: ignore[attr-defined]
            return_value=decision
        )
        enforce_module.gather_enforce_context = MagicMock(  # type: ignore[attr-defined]
            return_value=("ENFORCE_PROMPT_BODY", []),
        )

    def test_returns_dispatch_request_for_need_cloud_match(self) -> None:
        from lib.builders import build_enforce_request
        from lib.routing import WORKFLOW_ENFORCE_PR_ISSUE_STATE

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")
        payload = {
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 9},
            "pull_request": {"number": 21},
            "sender": {"login": "alice"},
        }

        request = build_enforce_request(
            payload,
            github_client=github_client,
            workspace_path=Path("/tmp/ws"),
        )
        self.assertEqual(request.workflow, WORKFLOW_ENFORCE_PR_ISSUE_STATE)
        self.assertIsNone(request.skill_name)
        self.assertEqual(request.prompt, "ENFORCE_PROMPT_BODY")
        self.assertEqual(request.payload_subset["pr_number"], 21)
        self.assertEqual(request.payload_subset["change_kind"], "implementation")
        # ``enforce_pr_state_synchronously`` already drove ``progress.start``
        # for this workflow; the builder only needs to capture the
        # resulting comment id (here returned by the MagicMock factory).
        self.assertEqual(request.payload_subset["progress_comment_id"], 4242)
        self.assertEqual(request.payload_subset["progress_run_id"], "run-uuid-hex")

    def test_raises_when_decision_is_not_need_cloud_match(self) -> None:
        from lib.builders import build_enforce_request

        # Override the helper to return an ``allow`` decision; the
        # builder should refuse to dispatch in that case.
        scripts = sys.modules["scripts"]
        scripts.enforce_pr_issue_state.enforce_pr_state_synchronously.return_value = SimpleNamespace(  # type: ignore[attr-defined]
            action="allow",
            allow_review=True,
            reason="markdown-only",
            close_comment="",
            context=None,
        )

        payload = {
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 9},
            "pull_request": {"number": 21},
            "sender": {"login": "alice"},
        }
        with self.assertRaises(RuntimeError):
            build_enforce_request(
                payload,
                github_client=MagicMock(),
                workspace_path=Path("/tmp/ws"),
            )


class BuildBuilderRegistryTest(_BuilderTestBase):
    def test_registry_keys_match_workflow_constants(self) -> None:
        from lib.builders import build_builder_registry
        from lib.routing import (
            WORKFLOW_ENFORCE_PR_ISSUE_STATE,
            WORKFLOW_RESPOND_TO_PR_COMMENT,
            WORKFLOW_REVIEW_PR,
            WORKFLOW_VERIFY_PR_COMMENT,
        )

        registry = build_builder_registry(github_client_factory=lambda: MagicMock())
        self.assertEqual(
            set(registry.keys()),
            {
                WORKFLOW_REVIEW_PR,
                WORKFLOW_RESPOND_TO_PR_COMMENT,
                WORKFLOW_VERIFY_PR_COMMENT,
                WORKFLOW_ENFORCE_PR_ISSUE_STATE,
            },
        )


if __name__ == "__main__":
    unittest.main()
