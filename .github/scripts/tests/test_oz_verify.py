from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from oz_verify import (
    _build_skill_section,
    _extract_frontmatter_value,
    _extract_metadata_value,
    _format_media_embed,
    _frontmatter_declares_verification,
    _mime_type_from_filename,
    _read_frontmatter_block,
    build_consolidated_report,
    collect_media_artifacts,
    discover_verification_skills,
    resolve_media_download_urls,
    substitute_artifact_links,
)


def _write_skill(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


class FrontmatterParsingTest(unittest.TestCase):
    def test_reads_frontmatter_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            skill.write_text(
                "---\n"
                "name: verify-foo\n"
                "description: Foo.\n"
                "metadata:\n"
                '  verification: "true"\n'
                "---\n"
                "# body\n",
                encoding="utf-8",
            )
            block = _read_frontmatter_block(skill)
            self.assertIsNotNone(block)
            assert block is not None
            self.assertIn("name: verify-foo", block)
            self.assertIn("metadata:", block)
            self.assertIn('verification: "true"', block)

    def test_frontmatter_block_missing_when_no_opening_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            skill.write_text("# just markdown\n", encoding="utf-8")
            self.assertIsNone(_read_frontmatter_block(skill))

    def test_extract_frontmatter_value_handles_quotes_and_case(self) -> None:
        frontmatter = (
            'name: "verify-foo"\n'
            "Description: A thing\n"
            "metadata:\n"
            '  verification: "true"\n'
            "# comment: ignored\n"
        )
        self.assertEqual(_extract_frontmatter_value(frontmatter, "name"), "verify-foo")
        self.assertEqual(_extract_frontmatter_value(frontmatter, "description"), "A thing")
        # Nested scalars (e.g. metadata.verification) must NOT leak out via
        # the top-level accessor; callers need to use the metadata helper.
        self.assertEqual(_extract_frontmatter_value(frontmatter, "verification"), "")

    def test_extract_metadata_value_reads_nested_scalars(self) -> None:
        frontmatter = (
            "name: verify-foo\n"
            "description: Foo.\n"
            "metadata:\n"
            "  author: example-org\n"
            '  Verification: "TRUE"\n'
            "other: top-level\n"
            "  verification: ignored-when-not-in-metadata\n"
        )
        # Nested keys are resolved case-insensitively and trimmed of
        # surrounding quotes, matching the top-level helper's contract.
        self.assertEqual(
            _extract_metadata_value(frontmatter, "author"), "example-org"
        )
        self.assertEqual(
            _extract_metadata_value(frontmatter, "verification"), "TRUE"
        )
        # Indented lines that are not underneath `metadata:` must be
        # ignored; the helper is strict about scope.
        self.assertEqual(
            _extract_metadata_value(frontmatter, "missing"), ""
        )

    def test_extract_metadata_value_returns_empty_when_no_metadata_block(
        self,
    ) -> None:
        frontmatter = (
            "name: verify-foo\n"
            "description: Foo.\n"
            "verification: true\n"  # top-level verification must not count
        )
        self.assertEqual(
            _extract_metadata_value(frontmatter, "verification"), ""
        )

    def test_frontmatter_declares_verification_requires_metadata_true_literal(
        self,
    ) -> None:
        cases = [
            # Opted in: verification is "true" (string) or true (bare) under
            # the metadata map, in any case.
            ('metadata:\n  verification: "true"', True),
            ("metadata:\n  verification: true", True),
            ("metadata:\n  verification: True", True),
            ("metadata:\n  verification: 'true'", True),
            # Opted out: any non-true value under metadata.
            ("metadata:\n  verification: false", False),
            ("metadata:\n  verification: yes", False),
            # Not opted in: top-level verification key must be ignored now
            # that the tag lives under the metadata map.
            ("verification: true", False),
            # Not opted in: no verification tag at all.
            ("metadata:\n  author: example-org", False),
            ("", False),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for index, (block, expected) in enumerate(cases):
                with self.subTest(block=block):
                    skill = Path(tmp) / f"skill_{index}.md"
                    skill.write_text(
                        f"---\nname: verify-x\n{block}\n---\nbody\n",
                        encoding="utf-8",
                    )
                    self.assertEqual(
                        _frontmatter_declares_verification(skill), expected
                    )


class DiscoverVerificationSkillsTest(unittest.TestCase):
    def _setup_workspace(self, tmp: str) -> Path:
        root = Path(tmp)
        skills_dir = root / ".agents" / "skills"
        _write_skill(
            skills_dir / "verify-login" / "SKILL.md",
            (
                "---\n"
                "name: verify-login\n"
                "description: Login flow.\n"
                "metadata:\n"
                '  verification: "true"\n'
                "---\nbody\n"
            ),
        )
        _write_skill(
            skills_dir / "verify-signup" / "SKILL.md",
            (
                "---\n"
                "name: verify-signup\n"
                "description: Signup flow.\n"
                "metadata:\n"
                '  verification: "true"\n'
                "---\nbody\n"
            ),
        )
        _write_skill(
            skills_dir / "implement-issue" / "SKILL.md",
            "---\nname: implement-issue\ndescription: Implement.\n---\nbody\n",
        )
        # Opts out by using metadata.verification: false so we also cover
        # the "wrong value under metadata" rejection path.
        _write_skill(
            skills_dir / "verify-ignore" / "SKILL.md",
            (
                "---\n"
                "name: verify-ignore\n"
                "description: Opted out.\n"
                "metadata:\n"
                '  verification: "false"\n'
                "---\nbody\n"
            ),
        )
        # A skill that still uses the deprecated top-level form must NOT
        # be discovered — the tag now lives under the metadata map per
        # the Agent Skills spec.
        _write_skill(
            skills_dir / "verify-legacy" / "SKILL.md",
            "---\nname: verify-legacy\ndescription: Legacy flag.\nverification: true\n---\nbody\n",
        )
        return root

    def test_returns_only_opted_in_skills_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            skills = discover_verification_skills(root)
            self.assertEqual(
                [s["name"] for s in skills],
                ["verify-login", "verify-signup"],
            )
            self.assertEqual(skills[0]["description"], "Login flow.")

    def test_filter_narrows_results_by_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            skills = discover_verification_skills(root, skill_filter="verify-signup")
            self.assertEqual([s["name"] for s in skills], ["verify-signup"])

    def test_filter_misses_return_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self.assertEqual(
                discover_verification_skills(root, skill_filter="verify-missing"),
                [],
            )

    def test_missing_skills_directory_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(discover_verification_skills(Path(tmp)), [])


class MimeTypeTest(unittest.TestCase):
    def test_mime_type_from_filename_table(self) -> None:
        cases = [
            ("screen.png", "image/png"),
            ("shot.JPG", "image/jpeg"),
            ("clip.mov", "video/quicktime"),
            ("clip.webm", "video/webm"),
            ("file.txt", ""),
            ("", ""),
        ]
        for filename, expected in cases:
            with self.subTest(filename=filename):
                self.assertEqual(_mime_type_from_filename(filename), expected)


class CollectMediaArtifactsTest(unittest.TestCase):
    def test_collects_screenshots_and_media_files(self) -> None:
        # Build an artifact list matching the SDK shape (artifact_type on the
        # artifact, and nested data with artifact_uid/filename/mime_type).
        artifacts = [
            SimpleNamespace(
                artifact_type="SCREENSHOT",
                data=SimpleNamespace(
                    artifact_uid="uid-screenshot",
                    filename="",
                    mime_type="",
                ),
            ),
            SimpleNamespace(
                artifact_type="FILE",
                data=SimpleNamespace(
                    artifact_uid="uid-png",
                    filename="verify-login/success.png",
                    mime_type="image/png",
                ),
            ),
            SimpleNamespace(
                artifact_type="FILE",
                data=SimpleNamespace(
                    artifact_uid="uid-video",
                    filename="demo.mp4",
                    mime_type="",
                ),
            ),
            SimpleNamespace(
                artifact_type="FILE",
                data=SimpleNamespace(
                    artifact_uid="uid-report",
                    filename="verification_report.md",
                    mime_type="text/markdown",
                ),
            ),
            SimpleNamespace(
                artifact_type="PLAN",
                data=SimpleNamespace(document_uid="plan-uid"),
            ),
            SimpleNamespace(artifact_type="FILE", data=None),
        ]
        run = SimpleNamespace(artifacts=artifacts)
        collected = collect_media_artifacts(run)
        # Report markdown (text) must NOT be collected; screenshots and media
        # files with either an explicit mime type or a media extension must.
        uids = {entry["artifact_uid"] for entry in collected}
        self.assertEqual(uids, {"uid-screenshot", "uid-png", "uid-video"})
        screenshot = next(e for e in collected if e["artifact_uid"] == "uid-screenshot")
        self.assertTrue(screenshot["filename"].startswith("screenshot-"))
        self.assertEqual(screenshot["mime_type"], "image/png")
        video = next(e for e in collected if e["artifact_uid"] == "uid-video")
        self.assertEqual(video["mime_type"], "video/mp4")


class ResolveMediaDownloadUrlsTest(unittest.TestCase):
    def test_attaches_download_url_for_each_artifact(self) -> None:
        client = MagicMock()
        def _fake_get(uid: str) -> MagicMock:
            resp = MagicMock()
            resp.data = MagicMock()
            resp.data.download_url = f"https://signed.example/{uid}"
            return resp
        client.agent.get_artifact.side_effect = _fake_get

        artifacts = [
            {"artifact_uid": "uid-a", "filename": "a.png", "mime_type": "image/png"},
            {"artifact_uid": "uid-b", "filename": "b.mp4", "mime_type": "video/mp4"},
        ]
        resolved = resolve_media_download_urls(client, artifacts)
        self.assertEqual(len(resolved), 2)
        urls = {r["download_url"] for r in resolved}
        self.assertEqual(
            urls,
            {"https://signed.example/uid-a", "https://signed.example/uid-b"},
        )

    def test_drops_artifacts_without_signed_url(self) -> None:
        client = MagicMock()
        failing = MagicMock()
        failing.data = MagicMock()
        failing.data.download_url = None
        client.agent.get_artifact.return_value = failing
        resolved = resolve_media_download_urls(
            client, [{"artifact_uid": "uid", "filename": "a.png", "mime_type": "image/png"}]
        )
        self.assertEqual(resolved, [])

    def test_drops_artifacts_when_sdk_raises(self) -> None:
        client = MagicMock()
        client.agent.get_artifact.side_effect = RuntimeError("boom")
        resolved = resolve_media_download_urls(
            client, [{"artifact_uid": "uid", "filename": "a.png", "mime_type": "image/png"}]
        )
        self.assertEqual(resolved, [])


class SubstituteArtifactLinksTest(unittest.TestCase):
    def test_rewrites_markdown_image_and_link_urls(self) -> None:
        report = (
            "✅ Passed\n\n"
            "Here is the login screen:\n"
            "![login](login-success.png)\n\n"
            "And a demo: [watch it](demo.mp4)\n"
        )
        resolved = [
            {
                "artifact_uid": "uid-png",
                "filename": "login-success.png",
                "mime_type": "image/png",
                "download_url": "https://signed.example/png",
            },
            {
                "artifact_uid": "uid-mp4",
                "filename": "demo.mp4",
                "mime_type": "video/mp4",
                "download_url": "https://signed.example/mp4",
            },
        ]
        substituted, referenced = substitute_artifact_links(report, resolved)
        # Author's alt text/link text and image-vs-link form are preserved;
        # only the URL is rewritten to the signed download URL.
        self.assertIn("![login](https://signed.example/png)", substituted)
        self.assertIn("[watch it](https://signed.example/mp4)", substituted)
        self.assertEqual(
            referenced, {"login-success.png", "demo.mp4"}
        )

    def test_leaves_unmatched_links_alone(self) -> None:
        report = (
            "See [external docs](https://example.com/docs) and\n"
            "![missing](nope.png)."
        )
        substituted, referenced = substitute_artifact_links(report, [])
        self.assertEqual(substituted, report)
        self.assertEqual(referenced, set())

    def test_does_not_rewrite_absolute_urls(self) -> None:
        # Even when an artifact happens to share a filename with the trailing
        # path component of an absolute URL, the workflow must not rewrite
        # fully-qualified links the author already wrote.
        report = "[hosted elsewhere](https://cdn.example/login-success.png)"
        resolved = [
            {
                "artifact_uid": "uid",
                "filename": "login-success.png",
                "mime_type": "image/png",
                "download_url": "https://signed.example/x",
            }
        ]
        substituted, referenced = substitute_artifact_links(report, resolved)
        self.assertEqual(substituted, report)
        self.assertEqual(referenced, set())

    def test_resolves_basename_when_url_has_subdir_prefix(self) -> None:
        report = "![alt](verify-login/success.png)"
        resolved = [
            {
                "artifact_uid": "uid",
                "filename": "success.png",
                "mime_type": "image/png",
                "download_url": "https://signed.example/x",
            }
        ]
        substituted, referenced = substitute_artifact_links(report, resolved)
        self.assertIn("![alt](https://signed.example/x)", substituted)
        self.assertEqual(referenced, {"success.png"})

    def test_preserves_link_title(self) -> None:
        report = '![alt](shot.png "a title")'
        resolved = [
            {
                "artifact_uid": "uid",
                "filename": "shot.png",
                "mime_type": "image/png",
                "download_url": "https://signed.example/x",
            }
        ]
        substituted, _referenced = substitute_artifact_links(report, resolved)
        self.assertIn(
            '![alt](https://signed.example/x "a title")', substituted
        )


class FormatMediaEmbedTest(unittest.TestCase):
    def test_image_embed_uses_markdown_image_syntax(self) -> None:
        artifact = {
            "filename": "shot.png",
            "mime_type": "image/png",
            "download_url": "https://signed.example/shot.png",
        }
        self.assertEqual(
            _format_media_embed(artifact),
            "![shot.png](https://signed.example/shot.png)",
        )

    def test_video_embed_uses_html_video_tag(self) -> None:
        artifact = {
            "filename": "demo.mp4",
            "mime_type": "video/mp4",
            "download_url": "https://signed.example/demo.mp4",
        }
        self.assertEqual(
            _format_media_embed(artifact),
            '<video src="https://signed.example/demo.mp4" controls></video>',
        )


class BuildSkillSectionTest(unittest.TestCase):
    def test_section_includes_description_report_and_unreferenced_artifacts(self) -> None:
        result = {
            "skill": "verify-login",
            "description": "Login flow.",
            "report": "✅ Passed — login ok.",
            "artifacts": [
                {
                    "filename": "extra.png",
                    "mime_type": "image/png",
                    "download_url": "https://signed.example/extra.png",
                }
            ],
            "error": "",
            "session_link": "",
        }
        section = _build_skill_section(result)
        self.assertIn("### `verify-login`", section)
        self.assertIn("_Login flow._", section)
        self.assertIn("✅ Passed", section)
        self.assertIn("Additional artifacts", section)
        self.assertIn(
            "![extra.png](https://signed.example/extra.png)", section
        )

    def test_error_section_surfaces_failure(self) -> None:
        result = {
            "skill": "verify-login",
            "description": "",
            "report": "",
            "artifacts": [],
            "error": "agent run timed out",
            "session_link": "",
        }
        section = _build_skill_section(result)
        self.assertIn("❌", section)
        self.assertIn("agent run timed out", section)


class BuildConsolidatedReportTest(unittest.TestCase):
    def test_report_contains_header_footer_and_per_skill_sections(self) -> None:
        skills = [
            {"name": "verify-login", "directory": "verify-login", "description": ""},
            {"name": "verify-signup", "directory": "verify-signup", "description": ""},
        ]
        results = [
            {
                "skill": "verify-login",
                "description": "Login.",
                "report": "✅ Passed",
                "artifacts": [],
                "error": "",
                "session_link": "",
            },
            {
                "skill": "verify-signup",
                "description": "Signup.",
                "report": "❌ Failed",
                "artifacts": [],
                "error": "",
                "session_link": "",
            },
        ]
        body = build_consolidated_report(
            pr_number=42,
            requester="octocat",
            skill_filter="",
            workflow_run_url="https://github.com/o/r/actions/runs/1",
            skills_considered=skills,
            results=results,
        )
        self.assertIn("/oz-verify` report for PR #42", body)
        self.assertIn("Requested by @octocat", body)
        self.assertIn("Ran 2 verification skills", body)
        self.assertIn("`verify-login`", body)
        self.assertIn("`verify-signup`", body)
        self.assertIn("[view logs]", body)

    def test_report_calls_out_missing_skills(self) -> None:
        body = build_consolidated_report(
            pr_number=7,
            requester="",
            skill_filter="",
            workflow_run_url="",
            skills_considered=[],
            results=[],
        )
        self.assertIn("No verification skills were found", body)


if __name__ == "__main__":
    unittest.main()
