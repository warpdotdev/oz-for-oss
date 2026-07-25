from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from . import conftest  # noqa: F401

from oz.attachments import text_attachment
from oz.oz_client import (
    ROLE_DEFAULT,
    build_agent_config,
    dispatch_run,
    skill_display_name,
    skill_file_path,
    skill_spec,
)


def _write_skill(root: Path, name: str) -> Path:
    path = root / ".agents" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: test\n---\n", encoding="utf-8")
    return path


class SkillResolutionTest(unittest.TestCase):
    def test_skill_display_name_strips_qualified_repo_and_path(self) -> None:
        self.assertEqual(
            skill_display_name(
                "warpdotdev/common-skills:.agents/skills/implement-specs/SKILL.md"
            ),
            "implement-specs",
        )
        self.assertEqual(
            skill_display_name(".agents/skills/review-pr/SKILL.md"),
            "review-pr",
        )
        self.assertEqual(skill_display_name("write-tech-spec"), "write-tech-spec")

    def test_common_skill_resolution_uses_common_skills_repo_without_local_file(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                skill_file_path("implement-specs"),
                ".agents/skills/implement-specs/SKILL.md",
            )
            self.assertEqual(
                skill_spec("implement-specs"),
                "warpdotdev/common-skills:.agents/skills/implement-specs/SKILL.md",
            )
            self.assertEqual(
                skill_spec("check-impl-against-spec"),
                "warpdotdev/common-skills:.agents/skills/check-impl-against-spec/SKILL.md",
            )

    def test_local_skill_resolution_uses_workflow_repo_without_github_actions_env(self) -> None:
        with tempfile.TemporaryDirectory() as workflow_dir:
            workflow_root = Path(workflow_dir)
            _write_skill(workflow_root, "implement-issue")

            with patch.dict(os.environ, {}, clear=True), patch(
                "oz.oz_client._workflow_code_root",
                return_value=workflow_root,
            ):
                self.assertEqual(
                    skill_file_path("implement-issue"),
                    ".agents/skills/implement-issue/SKILL.md",
                )
                self.assertEqual(
                    skill_spec("implement-issue"),
                    "warpdotdev/oz-for-oss:.agents/skills/implement-issue/SKILL.md",
                )

    def test_github_actions_env_vars_do_not_override_workflow_skill(self) -> None:
        with tempfile.TemporaryDirectory() as workflow_dir, tempfile.TemporaryDirectory() as workspace_dir:
            workflow_root = Path(workflow_dir)
            workspace_root = Path(workspace_dir)
            _write_skill(workflow_root, "implement-issue")
            _write_skill(workspace_root, "implement-issue")

            with patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY": "acme/widgets",
                    "GITHUB_WORKSPACE": workspace_root.as_posix(),
                },
                clear=True,
            ), patch(
                "oz.oz_client._workflow_code_root",
                return_value=workflow_root,
            ):
                self.assertEqual(
                    skill_file_path("implement-issue"),
                    ".agents/skills/implement-issue/SKILL.md",
                )
                self.assertEqual(
                    skill_spec("implement-issue"),
                    "warpdotdev/oz-for-oss:.agents/skills/implement-issue/SKILL.md",
                )

    def test_workflow_code_repository_env_var_selects_skill_repo(self) -> None:
        with tempfile.TemporaryDirectory() as workflow_dir:
            workflow_root = Path(workflow_dir)
            _write_skill(workflow_root, "implement-issue")

            with patch.dict(
                os.environ,
                {"WORKFLOW_CODE_REPOSITORY": "forks/oz-for-oss"},
                clear=True,
            ), patch(
                "oz.oz_client._workflow_code_root",
                return_value=workflow_root,
            ):
                self.assertEqual(
                    skill_spec("implement-issue"),
                    "forks/oz-for-oss:.agents/skills/implement-issue/SKILL.md",
                )

    def test_common_skills_repository_env_var_selects_common_skill_repo(self) -> None:
        with patch.dict(
            os.environ,
            {"COMMON_SKILLS_REPOSITORY": "forks/common-skills"},
            clear=True,
        ):
            self.assertEqual(
                skill_spec("review-pr"),
                "forks/common-skills:.agents/skills/review-pr/SKILL.md",
            )


class _FakeAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(run_id="oz-run-1")


class _FakeClient:
    def __init__(self) -> None:
        self.agent = _FakeAgent()


class DispatchRunAttachmentTest(unittest.TestCase):
    def test_dispatch_run_includes_attachments_when_provided(self) -> None:
        client = _FakeClient()
        attachment = text_attachment(
            file_name="context.txt",
            text="hello from an attachment",
        )

        response = dispatch_run(
            prompt="prompt body",
            skill_name=None,
            title="Attachment test",
            config={"environment_id": "env", "name": "attachment-test"},
            attachments=[attachment],
            client=client,  # type: ignore[arg-type]
        )

        self.assertEqual(response.run_id, "oz-run-1")
        self.assertEqual(client.agent.calls[0]["attachments"], (attachment,))

    def test_dispatch_run_omits_attachments_when_empty(self) -> None:
        client = _FakeClient()

        dispatch_run(
            prompt="prompt body",
            skill_name=None,
            title="Attachment test",
            config={"environment_id": "env", "name": "attachment-test"},
            attachments=[],
            client=client,  # type: ignore[arg-type]
        )

        self.assertNotIn("attachments", client.agent.calls[0])


class BuildAgentConfigComputerUseTest(unittest.TestCase):
    """Computer use is an opt-in, env-gated per-run capability (REMOTE-2280).

    ``build_agent_config`` must omit ``computer_use_enabled`` by default so the
    OSS template never silently requires the warp-xvfb-sidecar, and must set it
    to ``True`` only when ``WARP_COMPUTER_USE_ENABLED`` is truthy.
    """

    def _build(self, env: dict[str, str]) -> dict[str, Any]:
        with patch.dict(os.environ, env, clear=True):
            return build_agent_config(
                config_name="review-pull-request",
                workspace=Path("/tmp/workspace"),
                role=ROLE_DEFAULT,
            )

    def test_omits_computer_use_enabled_by_default(self) -> None:
        config = self._build({"WARP_ENVIRONMENT_ID": "env-1"})
        self.assertEqual(config["environment_id"], "env-1")
        self.assertEqual(config["name"], "review-pull-request")
        self.assertNotIn("computer_use_enabled", config)

    def test_sets_computer_use_enabled_when_flag_is_truthy(self) -> None:
        config = self._build(
            {
                "WARP_ENVIRONMENT_ID": "env-1",
                "WARP_COMPUTER_USE_ENABLED": "true",
            }
        )
        self.assertIs(config["computer_use_enabled"], True)

    def test_truthy_values_enable_computer_use(self) -> None:
        for value in ("1", "true", "TRUE"):
            config = self._build(
                {
                    "WARP_ENVIRONMENT_ID": "env-1",
                    "WARP_COMPUTER_USE_ENABLED": value,
                }
            )
            self.assertIs(
                config["computer_use_enabled"], True, msg=f"value={value!r}"
            )

    def test_falsy_values_keep_computer_use_off(self) -> None:
        for value in ("0", "false", "yes", "on", "garbage", ""):
            config = self._build(
                {
                    "WARP_ENVIRONMENT_ID": "env-1",
                    "WARP_COMPUTER_USE_ENABLED": value,
                }
            )
            self.assertNotIn(
                "computer_use_enabled", config, msg=f"value={value!r}"
            )


if __name__ == "__main__":
    unittest.main()
