from __future__ import annotations

import unittest
from types import SimpleNamespace

from . import conftest  # noqa: F401

from oz.verification import discover_verification_skills_from_repo


class DiscoverVerificationSkillsFromRepoTest(unittest.TestCase):
    def test_discovers_verification_skills_from_repo_contents(self) -> None:
        entries = [
            SimpleNamespace(type="dir", path=".agents/skills/verify-ui"),
            SimpleNamespace(type="dir", path=".agents/skills/not-verification"),
            SimpleNamespace(type="file", path=".agents/skills/README.md"),
        ]
        files = {
            ".agents/skills/verify-ui/SKILL.md": SimpleNamespace(
                decoded_content=b"""---
name: verify-ui
description: Check the UI
metadata:
  verification: true
---
body
"""
            ),
            ".agents/skills/not-verification/SKILL.md": SimpleNamespace(
                decoded_content=b"""---
name: not-verification
metadata:
  verification: false
---
body
"""
            ),
        }

        class Repo:
            def get_contents(self, path: str):
                if path == ".agents/skills":
                    return entries
                return files[path]

        discovered = discover_verification_skills_from_repo(Repo())
        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].name, "verify-ui")
        self.assertEqual(
            discovered[0].path.as_posix(),
            ".agents/skills/verify-ui/SKILL.md",
        )
        self.assertEqual(discovered[0].description, "Check the UI")

    def test_returns_empty_list_when_repo_skills_are_unavailable(self) -> None:
        class Repo:
            def get_contents(self, path: str):
                raise RuntimeError("not found")

        self.assertEqual(discover_verification_skills_from_repo(Repo()), [])


if __name__ == "__main__":
    unittest.main()
