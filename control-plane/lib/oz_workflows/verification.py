from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, cast

import yaml
from oz_agent_sdk.types.agent import RunItem

from .oz_client import build_oz_client

_FRONTMATTER_PATTERN = re.compile(
    r"\A\s*---\s*\n(?P<frontmatter>.*?)\n---\s*(?:\n|$)",
    re.DOTALL,
)
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}


@dataclass(frozen=True)
class VerificationSkill:
    name: str
    path: Path
    description: str


@dataclass(frozen=True)
class VerificationArtifact:
    artifact_type: str
    title: str
    content_type: str
    download_url: str
    description: str

    @property
    def is_image(self) -> bool:
        if self.artifact_type == "SCREENSHOT":
            return True
        if self.content_type.lower().startswith("image/"):
            return True
        return Path(self.title).suffix.lower() in _IMAGE_EXTENSIONS

    @property
    def is_video(self) -> bool:
        if self.content_type.lower().startswith("video/"):
            return True
        return Path(self.title).suffix.lower() in _VIDEO_EXTENSIONS


def _load_frontmatter(path: Path) -> dict[str, Any]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    match = _FRONTMATTER_PATTERN.match(raw_text)
    if match is None:
        return {}
    try:
        payload = yaml.safe_load(match.group("frontmatter")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _frontmatter_metadata_flag(frontmatter: dict[str, Any], flag_name: str) -> bool:
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return False
    value = metadata.get(flag_name)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def discover_verification_skills(workspace_root: Path) -> list[VerificationSkill]:
    skills_root = Path(workspace_root) / ".agents" / "skills"
    if not skills_root.is_dir():
        return []
    discovered: list[VerificationSkill] = []
    for skill_path in sorted(skills_root.glob("*/SKILL.md")):
        frontmatter = _load_frontmatter(skill_path)
        if not _frontmatter_metadata_flag(frontmatter, "verification"):
            continue
        name = str(frontmatter.get("name") or skill_path.parent.name).strip()
        if not name:
            name = skill_path.parent.name
        description = str(frontmatter.get("description") or "").strip()
        discovered.append(
            VerificationSkill(
                name=name,
                path=skill_path.resolve(),
                description=description,
            )
        )
    return discovered


def format_verification_skills_for_prompt(
    skills: list[VerificationSkill], *, workspace_root: Path
) -> str:
    if not skills:
        return "- None"
    lines: list[str] = []
    root = Path(workspace_root).resolve()
    for skill in skills:
        try:
            display_path = skill.path.relative_to(root).as_posix()
        except ValueError:
            display_path = skill.path.as_posix()
        description = f" — {skill.description}" if skill.description else ""
        lines.append(f"- `{skill.name}` at `{display_path}`{description}")
    return "\n".join(lines)


def _artifact_field(value: Any, name: str, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, dict):
        result = value.get(name, default)
    else:
        result = getattr(value, name, default)
    if result is None:
        return default
    return str(result)


def list_downloadable_verification_artifacts(
    run: RunItem,
    *,
    exclude_filenames: set[str] | None = None,
) -> list[VerificationArtifact]:
    client = None
    excluded = {name for name in (exclude_filenames or set()) if name}
    collected: list[VerificationArtifact] = []
    seen: set[tuple[str, str, str]] = set()
    for artifact in cast(list[Any], run.artifacts or []):
        artifact_type = _artifact_field(artifact, "artifact_type").upper()
        if artifact_type in {"PLAN", "PULL_REQUEST"}:
            continue
        data = getattr(artifact, "data", None)
        filename = _artifact_field(data, "filename").strip()
        if filename and filename in excluded:
            continue
        artifact_uid = _artifact_field(data, "artifact_uid").strip()
        response_type = artifact_type
        response_data = data
        download_url = _artifact_field(response_data, "download_url").strip()
        if artifact_uid and not download_url:
            if client is None:
                client = build_oz_client()
            try:
                response = client.agent.get_artifact(artifact_uid)
            except Exception:
                continue
            response_type = _artifact_field(response, "artifact_type", artifact_type).upper()
            response_data = getattr(response, "data", None)
            download_url = _artifact_field(response_data, "download_url").strip()
        if not download_url:
            continue
        content_type = _artifact_field(response_data, "content_type").strip()
        description = _artifact_field(response_data, "description").strip()
        response_filename = _artifact_field(response_data, "filename").strip()
        title = response_filename or filename or description or artifact_type.title()
        key = (response_type, title, download_url)
        if key in seen:
            continue
        seen.add(key)
        collected.append(
            VerificationArtifact(
                artifact_type=response_type,
                title=title,
                content_type=content_type,
                download_url=download_url,
                description=description,
            )
        )
    return collected


def render_verification_comment(
    report: dict[str, Any],
    *,
    session_link: str = "",
    artifacts: Iterable[VerificationArtifact] = (),
) -> str:
    overall_status = str(report.get("overall_status") or "mixed").strip().lower()
    if overall_status not in {"passed", "failed", "mixed"}:
        overall_status = "mixed"
    summary = str(report.get("summary") or "").strip()
    raw_skills = report.get("skills")
    skills: list[dict[str, str]] = []
    if isinstance(raw_skills, list):
        for entry in raw_skills:
            if not isinstance(entry, dict):
                continue
            skills.append(
                {
                    "name": str(entry.get("name") or "").strip(),
                    "path": str(entry.get("path") or "").strip(),
                    "status": str(entry.get("status") or "").strip().lower(),
                    "summary": str(entry.get("summary") or "").strip(),
                }
            )

    sections = [f"## /oz-verify report\nStatus: **{overall_status}**"]
    if session_link.strip():
        sections.append(f"Session: [view on Warp]({session_link.strip()})")
    if summary:
        sections.append(f"## Summary\n{summary}")
    if skills:
        lines = []
        for skill in skills:
            name = skill["name"] or "unnamed-skill"
            path = skill["path"]
            status = skill["status"] or "mixed"
            detail = skill["summary"]
            prefix = f"- `{name}`"
            if path:
                prefix += f" (`{path}`)"
            prefix += f": **{status}**"
            if detail:
                prefix += f" — {detail}"
            lines.append(prefix)
        sections.append("## Skill results\n" + "\n".join(lines))

    artifact_list = list(artifacts)
    screenshots = [artifact for artifact in artifact_list if artifact.is_image]
    videos = [artifact for artifact in artifact_list if artifact.is_video and not artifact.is_image]
    others = [
        artifact
        for artifact in artifact_list
        if artifact not in screenshots and artifact not in videos
    ]

    if screenshots:
        screenshot_blocks = []
        for artifact in screenshots:
            alt_text = artifact.description or artifact.title or "Verification screenshot"
            screenshot_blocks.append(f"### {artifact.title}\n![{alt_text}]({artifact.download_url})")
        sections.append("## Screenshots\n" + "\n\n".join(screenshot_blocks))
    if videos:
        video_lines = []
        for artifact in videos:
            label = artifact.description or artifact.title
            video_lines.append(f"- [{label}]({artifact.download_url})")
        sections.append("## Video artifacts\n" + "\n".join(video_lines))
    if others:
        other_lines = []
        for artifact in others:
            label = artifact.description or artifact.title
            other_lines.append(f"- [{label}]({artifact.download_url})")
        sections.append("## Additional artifacts\n" + "\n".join(other_lines))

    return "\n\n".join(section.strip() for section in sections if section.strip())
