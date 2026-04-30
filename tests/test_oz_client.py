from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import conftest  # noqa: F401

from oz_workflows.oz_client import skill_file_path, skill_spec


def _write_skill(root: Path, name: str) -> Path:
    path = root / ".agents" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: test\n---\n", encoding="utf-8")
    return path


class SkillResolutionTest(unittest.TestCase):
    def test_skill_resolution_without_github_repository_uses_workflow_repo(self) -> None:
        with tempfile.TemporaryDirectory() as workflow_dir, tempfile.TemporaryDirectory() as workspace_dir:
            workflow_root = Path(workflow_dir)
            workspace_root = Path(workspace_dir)
            skill_path = _write_skill(workflow_root, "implement-specs")

            with patch.dict(os.environ, {}, clear=True), patch(
                "oz_workflows.oz_client._workflow_code_root",
                return_value=workflow_root,
            ), patch("oz_workflows.oz_client.workspace", return_value=workspace_root):
                self.assertEqual(skill_file_path("implement-specs"), skill_path.as_posix())
                self.assertEqual(
                    skill_spec("implement-specs"),
                    "warpdotdev/oz-for-oss:.agents/skills/implement-specs/SKILL.md",
                )

    def test_consumer_repo_skill_override_still_wins_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as workflow_dir, tempfile.TemporaryDirectory() as workspace_dir:
            workflow_root = Path(workflow_dir)
            workspace_root = Path(workspace_dir)
            _write_skill(workflow_root, "review-pr")
            _write_skill(workspace_root, "review-pr")

            with patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY": "acme/widgets",
                    "GITHUB_WORKSPACE": workspace_root.as_posix(),
                },
                clear=True,
            ), patch(
                "oz_workflows.oz_client._workflow_code_root",
                return_value=workflow_root,
            ), patch("oz_workflows.oz_client.workspace", return_value=workspace_root):
                self.assertEqual(
                    skill_file_path("review-pr"),
                    ".agents/skills/review-pr/SKILL.md",
                )
                self.assertEqual(
                    skill_spec("review-pr"),
                    "acme/widgets:.agents/skills/review-pr/SKILL.md",
                )


if __name__ == "__main__":
    unittest.main()
