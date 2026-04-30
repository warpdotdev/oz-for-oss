"""Tests for ``control_plane.core.builders``.

The builders are thin wrappers around the workflow-specific
``gather_*_context`` / ``build_*_prompt`` helpers in ``core/workflows``.
The tests stub each gather/build helper so the assertions stay focused
on builder wiring (payload parsing, repo handle resolution,
DispatchRequest shape).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType
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
    if name == "oz":
        module.__path__ = [str(Path(__file__).resolve().parent.parent / "oz")]  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


class _BuilderTestBase(unittest.TestCase):
    """Mixin that owns the stub modules the builders import lazily."""

    def setUp(self) -> None:
        super().setUp()
        self._module_keys = [
            "workflows",
            "workflows.review_pr",
            "workflows.respond_to_pr_comment",
            "workflows.verify_pr_comment",
            "workflows.triage_new_issues",
            "workflows.create_spec_from_issue",
            "workflows.create_implementation_from_issue",
            "oz",
            "oz.agent_workflow",
            "oz.helpers",
        ]
        self._original_modules = {
            key: sys.modules.get(key) for key in self._module_keys
        }
        # The builders import :class:`WorkflowProgressComment` and the
        # workflow-specific ``format_*_start_line`` helpers lazily to
        # avoid pulling PyGithub into the test path. Stub the helper
        # module so each test can drive the lifecycle without going
        # through the production helper.
        oz = _ensure_module("oz")
        helpers = _ensure_module("oz.helpers")
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
        helpers.format_triage_start_line = MagicMock(  # type: ignore[attr-defined]
            return_value="I'm starting to work on triaging this issue."
        )
        helpers.triggering_comment_prompt_text = MagicMock(  # type: ignore[attr-defined]
            return_value=""
        )
    def assert_deferred_progress(
        self,
        request: Any,
        *,
        start_line: str | None = None,
        expect_start: bool = True,
        expect_existing_comment_update: bool = False,
    ) -> None:
        self.assertNotIn("progress_run_id", request.payload_subset)
        self.assertIsNotNone(request.on_dispatched)
        before_count = len(self.progress_instances)
        updates = request.on_dispatched("oz-run-123")
        self.assertEqual(updates, {"progress_comment_id": 4242})
        self.assertEqual(len(self.progress_instances), before_count + 1)
        helpers = sys.modules["oz.helpers"]
        self.assertEqual(
            helpers.WorkflowProgressComment.call_args.kwargs["run_id"],  # type: ignore[attr-defined]
            "oz-run-123",
        )
        progress = self.progress_instances[-1]
        if expect_existing_comment_update:
            progress.start.assert_not_called()
            progress.record_oz_run_id.assert_called_once_with("oz-run-123")
        elif start_line is not None:
            progress.start.assert_called_once_with(start_line)
        elif expect_start:
            progress.start.assert_called_once()
        else:
            progress.start.assert_not_called()

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
        workflows = _ensure_module("workflows")
        review_module = _ensure_module("workflows.review_pr")
        workflows.review_pr = review_module  # type: ignore[attr-defined]
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
        from core.builders import build_review_request
        from core.routing import WORKFLOW_REVIEW_PR

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
        # Progress is created after the Oz run id is available.
        self.assertEqual(len(self.progress_instances), 0)
        self.assert_deferred_progress(request)

    def test_raises_when_payload_missing_installation_id(self) -> None:
        from core.builders import build_review_request

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
        workflows = _ensure_module("workflows")
        respond_module = _ensure_module("workflows.respond_to_pr_comment")
        workflows.respond_to_pr_comment = respond_module  # type: ignore[attr-defined]
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
        from core.builders import build_respond_request
        from core.routing import WORKFLOW_RESPOND_TO_PR_COMMENT

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
        # Progress lifecycle is deferred until the Oz run id is known.
        self.assertEqual(len(self.progress_instances), 0)
        self.assert_deferred_progress(request, start_line="I'm starting")
    def test_returns_dispatch_request_for_review_body(self) -> None:
        from core.builders import build_respond_request
        from core.routing import WORKFLOW_RESPOND_TO_PR_COMMENT

        github_client = MagicMock()
        repo = MagicMock(name="repo")
        github_client.get_repo.return_value = repo
        pr = MagicMock(name="pr")
        repo.get_pull.return_value = pr

        payload = {
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 1},
            "pull_request": {"number": 7},
            "review": {"id": 1234, "user": {"login": "alice"}},
        }

        request = build_respond_request(
            payload,
            github_client=github_client,
            workspace_path=Path("/tmp/ws"),
        )
        self.assertEqual(request.workflow, WORKFLOW_RESPOND_TO_PR_COMMENT)
        self.assertEqual(request.payload_subset["trigger_comment_id"], 999)
        respond_module = sys.modules["workflows.respond_to_pr_comment"]
        kwargs = respond_module.gather_pr_comment_context.call_args.kwargs  # type: ignore[attr-defined]
        self.assertEqual(kwargs["trigger_kind"], "review_body")
        self.assertEqual(kwargs["trigger_comment_id"], 1234)


class BuildVerifyRequestTest(_BuilderTestBase):
    def setUp(self) -> None:
        super().setUp()
        workflows = _ensure_module("workflows")
        verify_module = _ensure_module("workflows.verify_pr_comment")
        workflows.verify_pr_comment = verify_module  # type: ignore[attr-defined]
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
        from core.builders import build_verify_request
        from core.routing import WORKFLOW_VERIFY_PR_COMMENT

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
        self.assertEqual(len(self.progress_instances), 0)
        self.assert_deferred_progress(request)



class BuildTriageRequestTest(_BuilderTestBase):
    def setUp(self) -> None:
        super().setUp()
        workflows = _ensure_module("workflows")
        triage_module = _ensure_module("workflows.triage_new_issues")
        workflows.triage_new_issues = triage_module  # type: ignore[attr-defined]
        triage_module.gather_triage_context = MagicMock(  # type: ignore[attr-defined]
            return_value={
                "owner": "acme",
                "repo": "widgets",
                "issue_number": 91,
                "requester": "alice",
                "is_retriage": False,
                "issue_title": "Login broken",
                "issue_body": "It does not work.",
                "issue_labels": ["bug"],
                "issue_assignees": [],
                "issue_created_at": "2026-04-29T00:00:00Z",
                "triggering_comment_id": 0,
                "triggering_comment_text": "",
                "comments_text": "- none",
                "original_report": "",
                "triage_config": {"labels": {}},
                "template_context": {},
                "configured_labels": {},
                "repo_label_names": [],
            }
        )
        triage_module.build_triage_prompt_for_dispatch = MagicMock(  # type: ignore[attr-defined]
            return_value="TRIAGE_PROMPT_BODY"
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 4242},
            "issue": {"number": 91},
            "sender": {"login": "alice"},
        }

    def test_returns_dispatch_request_with_triage_prompt(self) -> None:
        from core.builders import build_triage_request
        from core.routing import WORKFLOW_TRIAGE_NEW_ISSUES

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")

        request = build_triage_request(
            self._payload(),
            github_client=github_client,
            workspace_path=Path("/tmp/ws"),
        )
        self.assertEqual(request.workflow, WORKFLOW_TRIAGE_NEW_ISSUES)
        self.assertEqual(request.repo, "acme/widgets")
        self.assertEqual(request.installation_id, 4242)
        self.assertEqual(request.title, "Triage issue #91")
        self.assertEqual(request.skill_name, "triage-issue")
        self.assertEqual(request.prompt, "TRIAGE_PROMPT_BODY")
        self.assertEqual(request.payload_subset["issue_number"], 91)
        self.assertEqual(len(self.progress_instances), 0)
        self.assert_deferred_progress(request)

    def test_raises_when_payload_is_missing_issue_number(self) -> None:
        from core.builders import build_triage_request

        payload = self._payload()
        payload.pop("issue")
        with self.assertRaises(ValueError):
            build_triage_request(
                payload,
                github_client=MagicMock(),
                workspace_path=Path("/tmp/ws"),
            )


class BuildPlanApprovedRequestTest(_BuilderTestBase):
    def setUp(self) -> None:
        super().setUp()
        workflows = _ensure_module("workflows")
        impl_module = _ensure_module("workflows.create_implementation_from_issue")
        workflows.create_implementation_from_issue = impl_module  # type: ignore[attr-defined]
        impl_module.IMPLEMENT_SPECS_SKILL = "implement-specs"  # type: ignore[attr-defined]
        impl_module.gather_create_implementation_context = MagicMock(  # type: ignore[attr-defined]
            return_value={
                "owner": "acme",
                "repo": "widgets",
                "issue_number": 91,
                "requester": "alice",
                "issue_title": "Add retry",
                "issue_labels": ["ready-to-implement"],
                "issue_assignees": ["oz-agent"],
                "target_branch": "oz-agent/spec-issue-91",
                "default_branch": "main",
                "spec_context_source": "approved-pr",
                "selected_spec_pr_number": 121,
                "selected_spec_pr_url": "https://github.com/acme/widgets/pull/121",
                "has_existing_implementation_pr": False,
                "spec_context_text": "Spec body",
                "coauthor_line": "",
                "coauthor_directives": "",
                "implement_specs_skill_path": ".agents/skills/implement-specs/SKILL.md",
                "spec_driven_implementation_skill_path": ".agents/skills/spec-driven-implementation/SKILL.md",
                "implement_issue_skill_path": ".agents/skills/implement-issue/SKILL.md",
                "progress_start_line": "I'm implementing this issue on top of the approved spec PR's branch.",
                "should_noop": False,
                "noop_reason": "",
                "progress_comment_id": 0,
            }
        )
        impl_module.build_create_implementation_prompt_for_dispatch = MagicMock(  # type: ignore[attr-defined]
            return_value="PLAN_APPROVED_IMPL_PROMPT"
        )
        helpers = sys.modules["oz.helpers"]
        helpers.resolve_issue_number_for_pr = MagicMock(  # type: ignore[attr-defined]
            return_value=91
        )

    def _payload(self, *, with_linked_issue: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": "labeled",
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": 1234},
            "label": {"name": "plan-approved"},
            "pull_request": {
                "number": 121,
                "state": "open",
                "head": {"ref": "oz-agent/spec-issue-91"},
                "base": {"ref": "main"},
                "user": {"login": "alice", "type": "User"},
            },
            "sender": {"login": "alice"},
        }
        if with_linked_issue:
            payload["linked_issue_number"] = 91
        return payload

    def test_returns_dispatch_request_using_stashed_issue_number(self) -> None:
        from core.builders import build_plan_approved_request
        from core.routing import WORKFLOW_PLAN_APPROVED

        github_client = MagicMock()
        github_client.get_repo.return_value = MagicMock(name="repo")

        request = build_plan_approved_request(
            self._payload(),
            github_client=github_client,
            workspace_path=Path("/tmp/ws"),
        )

        self.assertEqual(request.workflow, WORKFLOW_PLAN_APPROVED)
        self.assertEqual(request.repo, "acme/widgets")
        self.assertEqual(request.installation_id, 1234)
        # Dispatch reuses the create-implementation cloud config so the
        # cloud agent's environment/model defaults are unchanged.
        self.assertEqual(
            request.config_name, "create-implementation-from-issue"
        )
        self.assertEqual(request.title, "Implement issue #91 (plan-approved)")
        self.assertEqual(request.skill_name, "implement-specs")
        self.assertEqual(request.prompt, "PLAN_APPROVED_IMPL_PROMPT")
        self.assertEqual(request.payload_subset["issue_number"], 91)
        self.assertEqual(
            request.payload_subset["trigger_source"], "plan-approved"
        )
        self.assert_deferred_progress(
            request,
            start_line="I'm implementing this issue on top of the approved spec PR's branch.",
        )
        # The builder reuses the stashed linked_issue_number rather
        # than re-resolving the PR association.
        helpers = sys.modules["oz.helpers"]
        helpers.resolve_issue_number_for_pr.assert_not_called()  # type: ignore[attr-defined]

    def test_falls_back_to_resolving_when_linked_issue_missing(self) -> None:
        from core.builders import build_plan_approved_request

        github_client = MagicMock()
        repo_handle = MagicMock(name="repo")
        github_client.get_repo.return_value = repo_handle
        pr_obj = MagicMock(name="pr")
        pr_obj.get_files.return_value = [
            type("F", (), {"filename": "specs/GH91/product.md"})()
        ]
        repo_handle.get_pull.return_value = pr_obj

        request = build_plan_approved_request(
            self._payload(with_linked_issue=False),
            github_client=github_client,
            workspace_path=Path("/tmp/ws"),
        )
        self.assertEqual(request.payload_subset["issue_number"], 91)
        helpers = sys.modules["oz.helpers"]
        helpers.resolve_issue_number_for_pr.assert_called_once()  # type: ignore[attr-defined]

    def test_raises_when_linked_issue_cannot_be_resolved(self) -> None:
        from core.builders import build_plan_approved_request

        github_client = MagicMock()
        repo_handle = MagicMock(name="repo")
        github_client.get_repo.return_value = repo_handle
        pr_obj = MagicMock(name="pr")
        pr_obj.get_files.return_value = []
        repo_handle.get_pull.return_value = pr_obj
        helpers = sys.modules["oz.helpers"]
        helpers.resolve_issue_number_for_pr = MagicMock(return_value=None)  # type: ignore[attr-defined]

        with self.assertRaises(ValueError):
            build_plan_approved_request(
                self._payload(with_linked_issue=False),
                github_client=github_client,
                workspace_path=Path("/tmp/ws"),
            )


class BuildBuilderRegistryTest(_BuilderTestBase):
    def test_registry_keys_match_workflow_constants(self) -> None:
        from core.builders import build_builder_registry
        from core.routing import (
            WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
            WORKFLOW_CREATE_SPEC_FROM_ISSUE,
            WORKFLOW_PLAN_APPROVED,
            WORKFLOW_RESPOND_TO_PR_COMMENT,
            WORKFLOW_REVIEW_PR,
            WORKFLOW_TRIAGE_NEW_ISSUES,
            WORKFLOW_VERIFY_PR_COMMENT,
        )

        registry = build_builder_registry(github_client_factory=lambda: MagicMock())
        self.assertEqual(
            set(registry.keys()),
            {
                WORKFLOW_REVIEW_PR,
                WORKFLOW_RESPOND_TO_PR_COMMENT,
                WORKFLOW_VERIFY_PR_COMMENT,
                WORKFLOW_TRIAGE_NEW_ISSUES,
                WORKFLOW_CREATE_SPEC_FROM_ISSUE,
                WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
                WORKFLOW_PLAN_APPROVED,
            },
        )


if __name__ == "__main__":
    unittest.main()
