from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from triage_new_issues import format_issue_comments, format_recent_open_issues, resolve_issue_number_override

from oz_workflows.triage import (
    ORIGINAL_REPORT_END,
    ORIGINAL_REPORT_START,
    build_duplicate_comment,
    compose_triaged_issue_body,
    dedupe_strings,
    discover_issue_templates,
    extract_duplicate_issues,
    extract_original_issue_report,
    format_stakeholders_for_prompt,
    load_stakeholders,
    load_triage_config,
    select_recent_untriaged_issues,
)


class LoadTriageConfigTest(unittest.TestCase):
    def test_loads_valid_json_config(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                '{"labels":{"triaged":{"color":"0E8A16","description":"done"}}}',
                encoding="utf-8",
            )
            parsed = load_triage_config(config_path)
            self.assertIn("labels", parsed)
            self.assertNotIn("stakeholders", parsed)
            self.assertNotIn("default_experts", parsed)

    def test_loads_config_without_stakeholders_key(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                '{"labels":{"bug":{"color":"D73A4A","description":"bug"}}}',
                encoding="utf-8",
            )
            parsed = load_triage_config(config_path)
            self.assertIn("labels", parsed)

    def test_rejects_config_without_labels(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text('{"other": "value"}', encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_triage_config(config_path)


class LoadStakeholdersTest(unittest.TestCase):
    def test_parses_stakeholders_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "STAKEHOLDERS"
            path.write_text(
                "# Comment line\n"
                "/src/ @alice @bob\n"
                "\n"
                "/docs/ @carol\n",
                encoding="utf-8",
            )
            entries = load_stakeholders(path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["pattern"], "/src/")
            self.assertEqual(entries[0]["owners"], ["alice", "bob"])
            self.assertEqual(entries[1]["pattern"], "/docs/")
            self.assertEqual(entries[1]["owners"], ["carol"])

    def test_returns_empty_for_missing_file(self) -> None:
        entries = load_stakeholders(Path("/nonexistent/STAKEHOLDERS"))
        self.assertEqual(entries, [])

    def test_skips_lines_without_owners(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "STAKEHOLDERS"
            path.write_text("/src/\n/docs/ @alice\n", encoding="utf-8")
            entries = load_stakeholders(path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["pattern"], "/docs/")


class FormatStakeholdersForPromptTest(unittest.TestCase):
    def test_formats_entries(self) -> None:
        entries = [
            {"pattern": "/src/", "owners": ["alice", "bob"]},
            {"pattern": "/docs/", "owners": ["carol"]},
        ]
        result = format_stakeholders_for_prompt(entries)
        self.assertIn("/src/", result)
        self.assertIn("@alice", result)
        self.assertIn("@bob", result)
        self.assertIn("@carol", result)

    def test_returns_fallback_for_empty(self) -> None:
        result = format_stakeholders_for_prompt([])
        self.assertEqual(result, "No stakeholders configured.")


class DedupeStringsTest(unittest.TestCase):
    def test_preserves_order_while_deduplicating(self) -> None:
        self.assertEqual(dedupe_strings(["triaged", "bug", "triaged", "bug", "area:workflow"]), ["triaged", "bug", "area:workflow"])


class SelectRecentUntriagedIssuesTest(unittest.TestCase):
    def test_filters_old_triaged_and_pull_request_entries(self) -> None:
        cutoff = datetime(2026, 3, 24, 1, 0, tzinfo=timezone.utc)
        issues = [
            {
                "number": 1,
                "created_at": "2026-03-24T00:30:00Z",
                "labels": [],
            },
            {
                "number": 2,
                "created_at": "2026-03-24T01:15:00Z",
                "labels": [{"name": "triaged"}],
            },
            {
                "number": 3,
                "created_at": "2026-03-24T01:20:00Z",
                "labels": [],
                "pull_request": {"url": "https://example.test/pr/3"},
            },
            {
                "number": 4,
                "created_at": "2026-03-24T01:25:00Z",
                "labels": [{"name": "bug"}],
            },
        ]
        self.assertEqual(
            [issue["number"] for issue in select_recent_untriaged_issues(issues, cutoff=cutoff)],
            [4],
        )


class DiscoverIssueTemplatesTest(unittest.TestCase):
    def test_discovers_config_template_and_legacy_template(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            template_dir = workspace / ".github" / "ISSUE_TEMPLATE"
            template_dir.mkdir(parents=True)
            (template_dir / "config.yml").write_text("blank_issues_enabled: false\n", encoding="utf-8")
            (template_dir / "bug.yml").write_text("name: Bug Report\ndescription: File a bug\n", encoding="utf-8")
            (workspace / ".github" / "issue_template.md").write_text("---\nname: Legacy\nabout: Legacy template\n---\nBody", encoding="utf-8")
            result = discover_issue_templates(workspace)
            self.assertEqual(result["config"]["path"], ".github/ISSUE_TEMPLATE/config.yml")
            self.assertEqual(
                [template["path"] for template in result["templates"]],
                [".github/ISSUE_TEMPLATE/bug.yml", ".github/issue_template.md"],
            )


class PreservedOriginalReportTest(unittest.TestCase):
    def test_extracts_original_report_from_preserved_details_block(self) -> None:
        body = (
            "## Bug report\nStructured content\n\n"
            + ORIGINAL_REPORT_START
            + "\n<details>\n<summary>Original issue report</summary>\n\nOriginal report text\n\n</details>\n"
            + ORIGINAL_REPORT_END
        )
        self.assertEqual(extract_original_issue_report(body), "Original report text")

    def test_composes_visible_body_with_preserved_original_report(self) -> None:
        updated = compose_triaged_issue_body("## Bug report\nStructured content", "Original report text")
        self.assertIn("## Bug report\nStructured content", updated)
        self.assertIn(ORIGINAL_REPORT_START, updated)
        self.assertIn(ORIGINAL_REPORT_END, updated)
        self.assertIn("<summary>Original issue report</summary>", updated)
        self.assertIn("Original report text", updated)

class ResolveIssueNumberOverrideTest(unittest.TestCase):
    def test_uses_issue_number_from_issue_comment_event(self) -> None:
        self.assertEqual(
            resolve_issue_number_override("issue_comment", {"issue": {"number": 42}}),
            "42",
        )

    def test_uses_issue_number_from_issue_opened_event(self) -> None:
        self.assertEqual(
            resolve_issue_number_override("issues", {"issue": {"number": 84}}),
            "84",
        )


class FormatIssueCommentsTest(unittest.TestCase):
    def test_can_exclude_triggering_comment(self) -> None:
        rendered = format_issue_comments(
            [
                {
                    "id": 1,
                    "author_association": "MEMBER",
                    "created_at": "2026-03-24T00:00:00Z",
                    "body": "Earlier context",
                    "user": {"login": "alice"},
                },
                {
                    "id": 2,
                    "author_association": "MEMBER",
                    "created_at": "2026-03-24T01:00:00Z",
                    "body": "@oz-agent focus on repro",
                    "user": {"login": "alice"},
                },
            ],
            exclude_comment_id=2,
        )
        self.assertEqual(rendered, "- @alice [MEMBER] (2026-03-24T00:00:00Z): Earlier context")


class ExtractDuplicateIssuesTest(unittest.TestCase):
    def test_returns_numbers_from_valid_list(self) -> None:
        result = {"duplicate_of": [10, 20]}
        self.assertEqual(extract_duplicate_issues(result), [10, 20])

    def test_returns_empty_when_missing(self) -> None:
        self.assertEqual(extract_duplicate_issues({}), [])

    def test_returns_empty_for_non_list(self) -> None:
        self.assertEqual(extract_duplicate_issues({"duplicate_of": "not a list"}), [])

    def test_skips_invalid_entries(self) -> None:
        result = {"duplicate_of": [5, "bad", None, -1, 0, 42]}
        self.assertEqual(extract_duplicate_issues(result), [5, 42])

    def test_handles_string_numbers(self) -> None:
        result = {"duplicate_of": ["7", "12"]}
        self.assertEqual(extract_duplicate_issues(result), [7, 12])


class BuildDuplicateCommentTest(unittest.TestCase):
    def test_single_duplicate(self) -> None:
        comment = build_duplicate_comment([42])
        self.assertIn("#42", comment)
        self.assertIn("duplicate", comment.lower())
        self.assertIn("2 business days", comment)

    def test_multiple_duplicates(self) -> None:
        comment = build_duplicate_comment([10, 20])
        self.assertIn("#10", comment)
        self.assertIn("#20", comment)

    def test_empty_list_returns_empty(self) -> None:
        self.assertEqual(build_duplicate_comment([]), "")


class FormatRecentOpenIssuesTest(unittest.TestCase):
    def test_formats_issues_excluding_current(self) -> None:
        issues = [
            {"number": 1, "title": "First issue", "labels": [{"name": "bug"}]},
            {"number": 2, "title": "Second issue", "labels": []},
            {"number": 3, "title": "Third issue", "labels": [{"name": "enhancement"}]},
        ]
        result = format_recent_open_issues(issues, exclude_number=2)
        self.assertIn("#1", result)
        self.assertIn("First issue", result)
        self.assertIn("[bug]", result)
        self.assertNotIn("#2", result)
        self.assertIn("#3", result)

    def test_skips_pull_requests(self) -> None:
        issues = [
            {"number": 1, "title": "A PR", "labels": [], "pull_request": {"url": "x"}},
            {"number": 2, "title": "An issue", "labels": []},
        ]
        result = format_recent_open_issues(issues, exclude_number=0)
        self.assertNotIn("#1", result)
        self.assertIn("#2", result)

    def test_returns_none_for_empty(self) -> None:
        self.assertEqual(format_recent_open_issues([], exclude_number=1), "- None")

    def test_respects_limit(self) -> None:
        issues = [{"number": i, "title": f"Issue {i}", "labels": []} for i in range(1, 10)]
        result = format_recent_open_issues(issues, exclude_number=0, limit=3)
        self.assertEqual(result.count("- #"), 3)


if __name__ == "__main__":
    unittest.main()
