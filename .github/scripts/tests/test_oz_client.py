from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from pathlib import Path

from oz_workflows import oz_client
from oz_workflows.oz_client import (
    ROLE_DEFAULT,
    ROLE_REVIEW_TRIAGE,
    _resolve_environment_id,
    _workflow_code_root,
    build_agent_config,
    build_oz_client,
    skill_file_path,
    skill_spec,
)


class BuildOzClientTest(unittest.TestCase):
    def test_honors_base_url_env(self) -> None:
        """``build_oz_client`` forwards ``WARP_API_BASE_URL`` to the ``OzAPI``
        client and does not send any origin-token header.
        """
        env = {
            "WARP_API_KEY": "fake-key",
            "WARP_API_BASE_URL": "https://app.warp.dev/api/v1",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "oz_workflows.oz_client.OzAPI"
        ) as mock_oz_api:
            build_oz_client()
            _args, kwargs = mock_oz_api.call_args
            headers = kwargs["default_headers"]
            self.assertEqual(kwargs["base_url"], "https://app.warp.dev/api/v1")
            self.assertEqual(headers["x-oz-api-source"], "GITHUB_ACTION")
            self.assertNotIn("X-Warp-Origin-Token", headers)

    def test_requires_base_url(self) -> None:
        """Missing ``WARP_API_BASE_URL`` must surface as ``RuntimeError``."""
        env = {"WARP_API_KEY": "fake-key"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                build_oz_client()
            self.assertIn("WARP_API_BASE_URL", str(ctx.exception))


class SkillSpecTest(unittest.TestCase):
    def test_prefers_consuming_repo_skill_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir)
            workflow_root = repo_root / "__oz_shared"
            workflow_root.mkdir()
            skill_path = repo_root / ".agents/skills/implement-issue/SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text("# implement issue\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY": "warpdotdev/consumer-repo",
                    "GITHUB_WORKSPACE": str(repo_root),
                    "WORKFLOW_CODE_REPOSITORY": "warpdotdev/oz-for-oss",
                },
                clear=False,
            ), patch.object(oz_client, "_workflow_code_root", return_value=workflow_root):
                self.assertEqual(
                    skill_spec("implement-issue"),
                    "warpdotdev/consumer-repo:.agents/skills/implement-issue/SKILL.md",
                )
                self.assertEqual(
                    skill_file_path("implement-issue"),
                    ".agents/skills/implement-issue/SKILL.md",
                )

    def test_falls_back_to_workflow_repo_skill_when_consumer_repo_lacks_it(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir)
            workflow_root = repo_root / "__oz_shared"
            skill_path = workflow_root / ".agents/skills/implement-issue/SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text("# implement issue\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY": "warpdotdev/consumer-repo",
                    "GITHUB_WORKSPACE": str(repo_root),
                    "WORKFLOW_CODE_REPOSITORY": "warpdotdev/oz-for-oss",
                },
                clear=False,
            ), patch.object(oz_client, "_workflow_code_root", return_value=workflow_root):
                self.assertEqual(
                    skill_spec("implement-issue"),
                    "warpdotdev/oz-for-oss:.agents/skills/implement-issue/SKILL.md",
                )
                self.assertEqual(
                    skill_file_path("implement-issue"),
                    "__oz_shared/.agents/skills/implement-issue/SKILL.md",
                )

    def test_preserves_relative_skill_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir)
            skill_path = repo_root / ".agents/skills/review-pr/SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text("# review pr\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY": "warpdotdev/oz-oss-testbed",
                    "GITHUB_WORKSPACE": str(repo_root),
                },
                clear=False,
            ):
                self.assertEqual(
                    skill_spec(".agents/skills/review-pr/SKILL.md"),
                    "warpdotdev/oz-oss-testbed:.agents/skills/review-pr/SKILL.md",
                )
                self.assertEqual(
                    skill_file_path(".agents/skills/review-pr/SKILL.md"),
                    ".agents/skills/review-pr/SKILL.md",
                )

    def test_preserves_already_qualified_skill_spec(self) -> None:
        qualified = "warpdotdev/oz-oss-testbed:.agents/skills/create-tech-spec/SKILL.md"
        self.assertEqual(skill_spec(qualified), qualified)


class WorkflowCodeRootTest(unittest.TestCase):
    def test_walks_up_to_github_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir).resolve()
            nested = repo_root / ".github" / "scripts" / "oz_workflows"
            nested.mkdir(parents=True)
            fake_module = nested / "oz_client.py"
            fake_module.write_text("", encoding="utf-8")
            with patch.object(oz_client, "__file__", str(fake_module)):
                self.assertEqual(_workflow_code_root(), repo_root)

    def test_walks_up_when_module_is_nested_deeper(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir).resolve()
            nested = (
                repo_root
                / ".github"
                / "scripts"
                / "oz_workflows"
                / "extra"
                / "deeper"
            )
            nested.mkdir(parents=True)
            fake_module = nested / "oz_client.py"
            fake_module.write_text("", encoding="utf-8")
            with patch.object(oz_client, "__file__", str(fake_module)):
                self.assertEqual(_workflow_code_root(), repo_root)

    def test_raises_when_no_github_sentinel_found(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            nested = Path(tempdir).resolve() / "vendor" / "oz_workflows"
            nested.mkdir(parents=True)
            fake_module = nested / "oz_client.py"
            fake_module.write_text("", encoding="utf-8")
            with patch.object(oz_client, "__file__", str(fake_module)):
                with self.assertRaises(RuntimeError) as ctx:
                    _workflow_code_root()
                self.assertIn(".github", str(ctx.exception))


class BuildAgentConfigTest(unittest.TestCase):
    @patch.dict(os.environ, {"WARP_ENVIRONMENT_ID": "default-env"}, clear=False)
    def test_uses_warp_environment_id(self) -> None:
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
        )
        self.assertEqual(config["environment_id"], "default-env")

    @patch.dict(os.environ, {}, clear=True)
    def test_requires_warp_environment_id(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            build_agent_config(
                config_name="review-pull-request",
                workspace=Path("/tmp"),
            )
        self.assertIn("WARP_ENVIRONMENT_ID", str(ctx.exception))
        self.assertIn("oz environment list", str(ctx.exception))

    @patch.dict(os.environ, {"WARP_ENVIRONMENT_ID": "default-env"}, clear=True)
    def test_defaults_session_sharing_to_viewer(self) -> None:
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
        )
        self.assertEqual(
            dict(config).get("session_sharing"),
            {"public_access": "VIEWER"},
        )

    @patch.dict(
        os.environ,
        {
            "WARP_ENVIRONMENT_ID": "default-env",
            "WARP_SESSION_SHARING_PUBLIC_ACCESS": "EDITOR",
        },
        clear=True,
    )
    def test_session_sharing_can_be_set_to_editor(self) -> None:
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
        )
        self.assertEqual(
            dict(config).get("session_sharing"),
            {"public_access": "EDITOR"},
        )

    @patch.dict(
        os.environ,
        {
            "WARP_ENVIRONMENT_ID": "default-env",
            "WARP_SESSION_SHARING_PUBLIC_ACCESS": "viewer",
        },
        clear=True,
    )
    def test_session_sharing_is_case_insensitive(self) -> None:
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
        )
        self.assertEqual(
            dict(config).get("session_sharing"),
            {"public_access": "VIEWER"},
        )

    @patch.dict(
        os.environ,
        {
            "WARP_ENVIRONMENT_ID": "default-env",
            "WARP_SESSION_SHARING_PUBLIC_ACCESS": "none",
        },
        clear=True,
    )
    def test_session_sharing_can_be_disabled(self) -> None:
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
        )
        self.assertNotIn("session_sharing", dict(config))

    @patch.dict(
        os.environ,
        {
            "WARP_ENVIRONMENT_ID": "default-env",
            "WARP_SESSION_SHARING_PUBLIC_ACCESS": "off",
        },
        clear=True,
    )
    def test_session_sharing_off_also_disables(self) -> None:
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
        )
        self.assertNotIn("session_sharing", dict(config))

    @patch.dict(
        os.environ,
        {
            "WARP_ENVIRONMENT_ID": "default-env",
            "WARP_SESSION_SHARING_PUBLIC_ACCESS": "bogus",
        },
        clear=True,
    )
    def test_unknown_session_sharing_value_disables_sharing(self) -> None:
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
        )
        self.assertNotIn("session_sharing", dict(config))


class BuildAgentConfigRoleTest(unittest.TestCase):
    """Coverage for the ``role`` parameter on ``build_agent_config``.

    The review/triage agents (PR review, issue triage,
    ``respond-to-triaged-issue-comment``) optionally route onto a
    dedicated cloud environment via ``WARP_REVIEW_TRIAGE_ENVIRONMENT_ID``
    so an operator can give those workloads tighter resource limits than
    the default environment used by spec/implementation runs. When the
    review-triage variable is empty the resolver falls back to
    ``WARP_ENVIRONMENT_ID`` so deployments without the override behave
    identically to the legacy single-environment setup.
    """

    @patch.dict(
        os.environ,
        {
            "WARP_ENVIRONMENT_ID": "default-env",
            "WARP_REVIEW_TRIAGE_ENVIRONMENT_ID": "review-env",
        },
        clear=True,
    )
    def test_review_triage_role_prefers_review_triage_env(self) -> None:
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
            role=ROLE_REVIEW_TRIAGE,
        )
        self.assertEqual(config["environment_id"], "review-env")

    @patch.dict(
        os.environ,
        {"WARP_ENVIRONMENT_ID": "default-env"},
        clear=True,
    )
    def test_review_triage_role_falls_back_to_default_env(self) -> None:
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
            role=ROLE_REVIEW_TRIAGE,
        )
        self.assertEqual(config["environment_id"], "default-env")

    @patch.dict(
        os.environ,
        {
            "WARP_ENVIRONMENT_ID": "default-env",
            "WARP_REVIEW_TRIAGE_ENVIRONMENT_ID": "   ",
        },
        clear=True,
    )
    def test_blank_review_triage_env_falls_back_to_default(self) -> None:
        # ``optional_env`` already trims whitespace, so a value that's
        # only whitespace must behave the same as an unset variable.
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
            role=ROLE_REVIEW_TRIAGE,
        )
        self.assertEqual(config["environment_id"], "default-env")

    @patch.dict(
        os.environ,
        {
            "WARP_ENVIRONMENT_ID": "default-env",
            "WARP_REVIEW_TRIAGE_ENVIRONMENT_ID": "review-env",
        },
        clear=True,
    )
    def test_default_role_ignores_review_triage_env(self) -> None:
        config = build_agent_config(
            config_name="create-spec-from-issue",
            workspace=Path("/tmp"),
            role=ROLE_DEFAULT,
        )
        self.assertEqual(config["environment_id"], "default-env")

    @patch.dict(
        os.environ,
        {"WARP_REVIEW_TRIAGE_ENVIRONMENT_ID": "review-env"},
        clear=True,
    )
    def test_review_triage_role_works_without_default_env(self) -> None:
        # When only the review-triage variable is set, callers running
        # in that role must succeed without ``WARP_ENVIRONMENT_ID``.
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
            role=ROLE_REVIEW_TRIAGE,
        )
        self.assertEqual(config["environment_id"], "review-env")

    @patch.dict(os.environ, {}, clear=True)
    def test_review_triage_role_error_mentions_both_env_vars(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            build_agent_config(
                config_name="review-pull-request",
                workspace=Path("/tmp"),
                role=ROLE_REVIEW_TRIAGE,
            )
        message = str(ctx.exception)
        self.assertIn("WARP_REVIEW_TRIAGE_ENVIRONMENT_ID", message)
        self.assertIn("WARP_ENVIRONMENT_ID", message)

    @patch.dict(
        os.environ,
        {"WARP_ENVIRONMENT_ID": "default-env"},
        clear=True,
    )
    def test_unknown_role_uses_default_env_with_warning(self) -> None:
        # An unrecognized role does not raise: the resolver falls
        # through to ``WARP_ENVIRONMENT_ID`` and ``build_agent_config``
        # emits a `::warning::` annotation so an operator can spot the
        # typo without failing the workflow.
        with patch("oz_workflows.oz_client.warning") as warn_mock:
            config = build_agent_config(
                config_name="review-pull-request",
                workspace=Path("/tmp"),
                role="unknown-role",
            )
        self.assertEqual(config["environment_id"], "default-env")
        warn_mock.assert_called_once()
        warn_message = warn_mock.call_args.args[0]
        self.assertIn("unknown-role", warn_message)
        self.assertIn(ROLE_DEFAULT, warn_message)

    @patch.dict(
        os.environ,
        {
            "WARP_ENVIRONMENT_ID": "default-env",
            "WARP_REVIEW_TRIAGE_ENVIRONMENT_ID": "review-env",
        },
        clear=True,
    )
    def test_resolve_environment_id_dispatches_on_role(self) -> None:
        self.assertEqual(_resolve_environment_id(ROLE_REVIEW_TRIAGE), "review-env")
        self.assertEqual(_resolve_environment_id(ROLE_DEFAULT), "default-env")
        # Unknown roles use the default lookup so they still resolve to
        # ``WARP_ENVIRONMENT_ID`` rather than triggering an exception
        # inside the resolver.
        self.assertEqual(_resolve_environment_id("unknown"), "default-env")


if __name__ == "__main__":
    unittest.main()
