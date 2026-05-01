from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .helpers import get_field, parse_datetime

ORIGINAL_REPORT_START = "<!-- oz-agent-original-report-start -->"
ORIGINAL_REPORT_END = "<!-- oz-agent-original-report-end -->"
ISSUE_TEMPLATE_CONFIG_NAMES = {"config.yml", "config.yaml"}
ISSUE_TEMPLATE_SUFFIXES = {".md", ".yml", ".yaml"}
TRIAGE_SECTION_END = "<!-- oz-agent-triage-end -->"


def load_triage_config(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError("Issue triage config must be a JSON object")
    labels = parsed.get("labels")
    if not isinstance(labels, dict):
        raise RuntimeError("Issue triage config must include a labels object")
    return parsed


def load_stakeholders(path: Path) -> list[dict[str, Any]]:
    """Parse a CODEOWNERS-style STAKEHOLDERS file into structured entries.

    Each non-comment, non-blank line is expected to have the form:
        <pattern> @owner1 @owner2 ...

    Returns a list of dicts with ``pattern`` and ``owners`` keys.
    """
    entries: list[dict[str, Any]] = []
    if not path.exists():
        return entries
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern = parts[0]
        owners = [p.lstrip("@") for p in parts[1:] if p.startswith("@")]
        if owners:
            entries.append({"pattern": pattern, "owners": owners})
    return entries


def format_stakeholders_for_prompt(entries: list[dict[str, Any]]) -> str:
    """Format parsed STAKEHOLDERS entries into a human-readable prompt block."""
    if not entries:
        return "No stakeholders configured."
    lines: list[str] = []
    for entry in entries:
        owners = ", ".join(f"@{o}" for o in entry["owners"])
        lines.append(f"- {entry['pattern']} → {owners}")
    return "\n".join(lines)


def dedupe_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def issue_has_label(issue: Any, label_name: str) -> bool:
    for raw_label in get_field(issue, "labels", []):
        current = raw_label if isinstance(raw_label, str) else get_field(raw_label, "name")
        if current == label_name:
            return True
    return False


def select_recent_untriaged_issues(
    issues: list[Any],
    *,
    cutoff: datetime,
    triaged_label: str = "triaged",
) -> list[Any]:
    selected = [
        issue
        for issue in issues
        if not get_field(issue, "pull_request")
        and (
            get_field(issue, "created_at") >= cutoff
            if isinstance(get_field(issue, "created_at"), datetime)
            else parse_datetime(get_field(issue, "created_at") or "1970-01-01T00:00:00Z") >= cutoff
        )
        and not issue_has_label(issue, triaged_label)
    ]
    selected.sort(
        key=lambda issue: (
            get_field(issue, "created_at")
            if isinstance(get_field(issue, "created_at"), datetime)
            else parse_datetime(get_field(issue, "created_at") or "1970-01-01T00:00:00Z")
        )
    )
    return selected


def discover_issue_templates(workspace: Path) -> dict[str, Any]:
    template_dir = workspace / ".github" / "ISSUE_TEMPLATE"
    config: dict[str, str] | None = None
    templates: list[dict[str, str]] = []
    seen_template_paths: set[str] = set()

    def add_template(path: Path) -> None:
        key = str(path.resolve()).casefold()
        if key in seen_template_paths:
            return
        seen_template_paths.add(key)
        templates.append(
            {
                "path": path.relative_to(workspace).as_posix(),
                "content": path.read_text(encoding="utf-8").strip(),
            }
        )

    if template_dir.exists():
        for path in sorted(template_dir.iterdir()):
            if not path.is_file():
                continue
            if path.name.lower() in ISSUE_TEMPLATE_CONFIG_NAMES:
                config = {
                    "path": path.relative_to(workspace).as_posix(),
                    "content": path.read_text(encoding="utf-8").strip(),
                }
                continue
            if path.suffix.lower() not in ISSUE_TEMPLATE_SUFFIXES:
                continue
            add_template(path)

    for legacy_relative_path in [".github/issue_template.md", ".github/ISSUE_TEMPLATE.md"]:
        legacy_path = workspace / legacy_relative_path
        if not legacy_path.exists() or not legacy_path.is_file():
            continue
        add_template(legacy_path)

    return {
        "config": config,
        "templates": templates,
    }


def extract_original_issue_report(body: str) -> str:
    body = (body or "").strip()
    if ORIGINAL_REPORT_START not in body or ORIGINAL_REPORT_END not in body:
        return body
    start = body.index(ORIGINAL_REPORT_START) + len(ORIGINAL_REPORT_START)
    end = body.index(ORIGINAL_REPORT_END, start)
    report = body[start:end].strip()
    if report.startswith("<details>") and report.endswith("</details>"):
        inner = report.removeprefix("<details>").removesuffix("</details>").strip()
        summary = "<summary>Original issue report</summary>"
        if inner.startswith(summary):
            inner = inner.removeprefix(summary).strip()
        report = inner.strip()
    return report


def strip_preserved_original_report(body: str) -> str:
    text = (body or "").strip()
    if ORIGINAL_REPORT_START not in text or ORIGINAL_REPORT_END not in text:
        return text
    start = text.index(ORIGINAL_REPORT_START)
    end = text.index(ORIGINAL_REPORT_END, start) + len(ORIGINAL_REPORT_END)
    prefix = text[:start].rstrip()
    suffix = text[end:].lstrip()
    pieces = [piece for piece in [prefix, suffix] if piece]
    return "\n\n".join(pieces)


def build_original_report_details(original_report: str) -> str:
    report = original_report.strip() or "_No original issue report was provided._"
    return "\n".join(
        [
            ORIGINAL_REPORT_START,
            "<details>",
            "<summary>Original issue report</summary>",
            "",
            report,
            "",
            "</details>",
            ORIGINAL_REPORT_END,
        ]
    )


def compose_triaged_issue_body(visible_body: str, original_report: str) -> str:
    content = strip_preserved_original_report(visible_body)
    appendix = build_original_report_details(original_report)
    if not content:
        return appendix
    return f"{content}\n\n{appendix}"
