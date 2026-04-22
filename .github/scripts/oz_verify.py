from __future__ import annotations

import logging
import re
from contextlib import closing
from pathlib import Path
from textwrap import dedent
from typing import TypedDict

from github import Auth, Github
from github.PullRequest import PullRequest
from oz_agent_sdk import OzAPI
from oz_agent_sdk.types.agent import RunItem

from oz_workflows.artifacts import poll_for_text_artifact
from oz_workflows.env import optional_env, repo_parts, repo_slug, require_env, workspace
from oz_workflows.helpers import (
    POWERED_BY_SUFFIX,
    WorkflowProgressComment,
    build_next_steps_section,
    record_run_session_link,
)
from oz_workflows.oz_client import build_agent_config, build_oz_client, run_agent

logger = logging.getLogger(__name__)

# `/oz-verify` expects each verification skill to write a consolidated
# `verification_report.md` as a FILE artifact. The workflow polls for the
# artifact rather than scraping the working tree so the data is sourced
# authoritatively from the Oz run.
VERIFICATION_REPORT_FILENAME = "verification_report.md"

# Matches standard Markdown image or link constructs of the form
# ``![alt](url)`` or ``[text](url)``. The workflow rewrites any such URL
# that refers to an uploaded artifact filename with the artifact's signed
# download URL, so skills can reference evidence using plain Markdown.
_MARKDOWN_LINK_RE = re.compile(
    r"(?P<bang>!?)\[(?P<text>[^\]]*)\]\((?P<url>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)"
)

# Extensions used as a fallback when a FILE artifact's MIME type is missing
# or generic (e.g. `application/octet-stream`).
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm")


class MediaArtifact(TypedDict, total=False):
    """A screenshot/video artifact resolved to a signed download URL."""

    artifact_uid: str
    filename: str
    mime_type: str
    download_url: str


class VerificationResult(TypedDict, total=False):
    """Per-skill output collected by the workflow."""

    skill: str
    description: str
    report: str
    artifacts: list[MediaArtifact]
    error: str
    session_link: str


def _read_frontmatter_block(skill_path: Path) -> str | None:
    """Return the raw YAML frontmatter block for *skill_path*, if present."""
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    # Locate the closing `---` delimiter. Accept either `\n---\n` or a
    # trailing `\n---` at the end of the file.
    closing = text.find("\n---", 3)
    if closing == -1:
        return None
    return text[3:closing]


def _extract_frontmatter_value(frontmatter: str, key: str) -> str:
    """Return the value of a top-level scalar key from *frontmatter*.

    Handles simple quoted/unquoted scalars. Nested/mapping values are
    returned as-is so callers can decide how to interpret them.
    """
    pattern = re.compile(rf"^{re.escape(key)}\s*:\s*(.*?)\s*$", re.IGNORECASE)
    for raw in frontmatter.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        # Skip nested keys: top-level keys are never indented.
        if line[:1] in (" ", "\t"):
            continue
        match = pattern.match(line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    return ""


def _frontmatter_declares_verification(skill_path: Path) -> bool:
    """Return True when *skill_path*'s frontmatter has `verification: true`."""
    frontmatter = _read_frontmatter_block(skill_path)
    if frontmatter is None:
        return False
    value = _extract_frontmatter_value(frontmatter, "verification").lower()
    return value == "true"


def discover_verification_skills(
    workspace_root: Path, *, skill_filter: str = ""
) -> list[dict[str, str]]:
    """Return sorted verification-skill descriptors from *workspace_root*.

    A skill qualifies when its ``.agents/skills/<name>/SKILL.md`` declares
    ``verification: true`` in its YAML frontmatter. When *skill_filter* is
    provided, results are narrowed to the skill whose directory name or
    frontmatter ``name`` matches exactly.
    """
    skills_dir = workspace_root / ".agents" / "skills"
    if not skills_dir.is_dir():
        return []
    normalized_filter = (skill_filter or "").strip()
    results: list[dict[str, str]] = []
    for entry in sorted(skills_dir.iterdir()):
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file() or not _frontmatter_declares_verification(skill_md):
            continue
        frontmatter = _read_frontmatter_block(skill_md) or ""
        declared_name = _extract_frontmatter_value(frontmatter, "name") or entry.name
        description = _extract_frontmatter_value(frontmatter, "description")
        if normalized_filter and normalized_filter not in {entry.name, declared_name}:
            continue
        results.append(
            {
                "name": declared_name,
                "directory": entry.name,
                "description": description,
                "path": str(skill_md.resolve()),
            }
        )
    return results


def _mime_type_from_filename(filename: str) -> str:
    """Return a best-effort MIME type based on *filename*'s extension."""
    lower = (filename or "").lower()
    for ext in _IMAGE_EXTENSIONS:
        if lower.endswith(ext):
            suffix = "jpeg" if ext in {".jpg", ".jpeg"} else ext.lstrip(".")
            return f"image/{suffix}"
    for ext in _VIDEO_EXTENSIONS:
        if lower.endswith(ext):
            suffix = "quicktime" if ext == ".mov" else ext.lstrip(".")
            return f"video/{suffix}"
    return ""


def _is_media_mime(mime_type: str) -> bool:
    normalized = (mime_type or "").strip().lower()
    return normalized.startswith("image/") or normalized.startswith("video/")


def collect_media_artifacts(run: RunItem) -> list[MediaArtifact]:
    """Return SCREENSHOT/FILE artifacts from *run* that embed as media.

    SCREENSHOT artifacts are always included. FILE artifacts are included
    only when their MIME type (or filename extension, as a fallback) looks
    like an image or video so unrelated text artifacts (e.g.
    ``verification_report.md``) aren't rendered as broken embeds.
    """
    collected: list[MediaArtifact] = []
    for artifact in run.artifacts or []:
        artifact_type = getattr(artifact, "artifact_type", "")
        data = getattr(artifact, "data", None)
        if data is None:
            continue
        artifact_uid = str(getattr(data, "artifact_uid", "") or "")
        if not artifact_uid:
            continue
        filename = str(getattr(data, "filename", "") or "")
        mime_type = str(getattr(data, "mime_type", "") or "").strip()
        if artifact_type == "SCREENSHOT":
            # SCREENSHOT artifacts don't carry a filename; synthesize one
            # from the artifact UID so Markdown links can still reference
            # it and so unreferenced embeds have a stable alt-text key.
            effective_filename = filename or f"screenshot-{artifact_uid[:8]}"
            effective_mime = mime_type or "image/png"
            collected.append(
                {
                    "artifact_uid": artifact_uid,
                    "filename": effective_filename,
                    "mime_type": effective_mime,
                }
            )
            continue
        if artifact_type != "FILE":
            continue
        effective_mime = mime_type or _mime_type_from_filename(filename)
        if not _is_media_mime(effective_mime):
            continue
        collected.append(
            {
                "artifact_uid": artifact_uid,
                "filename": filename,
                "mime_type": effective_mime,
            }
        )
    return collected


def resolve_media_download_urls(
    client: OzAPI, artifacts: list[MediaArtifact]
) -> list[MediaArtifact]:
    """Fetch a signed ``download_url`` for every media artifact in *artifacts*.

    Artifacts that fail to resolve (network error, missing URL, etc.) are
    logged and dropped rather than raised so one broken artifact does not
    abort the workflow.
    """
    resolved: list[MediaArtifact] = []
    for artifact in artifacts:
        uid = artifact.get("artifact_uid", "")
        if not uid:
            continue
        try:
            response = client.agent.get_artifact(uid)
        except Exception:
            logger.exception(
                "Failed to resolve signed URL for artifact %s", uid
            )
            continue
        data = getattr(response, "data", None)
        download_url = str(getattr(data, "download_url", "") or "") if data else ""
        if not download_url:
            logger.warning("Artifact %s did not return a download_url", uid)
            continue
        resolved.append({**artifact, "download_url": download_url})
    return resolved


def _format_media_embed(artifact: MediaArtifact) -> str:
    """Return a markdown/HTML embed string for *artifact*.

    GitHub renders ``<video>`` tags in issue comments, and markdown image
    syntax for PNG/JPG/GIF, so this picks whichever matches the resolved
    MIME type (falling back to the filename extension when needed).
    """
    filename = artifact.get("filename") or "artifact"
    url = artifact.get("download_url") or ""
    mime = artifact.get("mime_type") or _mime_type_from_filename(filename)
    if mime.startswith("video/"):
        return f'<video src="{url}" controls></video>'
    return f"![{filename}]({url})"


def substitute_artifact_links(
    report: str, resolved: list[MediaArtifact]
) -> tuple[str, set[str]]:
    """Rewrite Markdown image/link URLs that refer to uploaded artifacts.

    Scans *report* for standard Markdown image (``![alt](url)``) and link
    (``[text](url)``) constructs. Whenever the URL matches the filename
    (or basename) of an uploaded artifact in *resolved*, the URL is
    replaced with the artifact's signed download URL; the surrounding
    Markdown (alt text, title, image vs. link form) is preserved so
    authors can choose how they want the evidence rendered.

    Returns ``(substituted_report, referenced_filenames)``. The returned
    set contains the filenames (as stored on the resolved artifact) of
    every artifact that was rewritten at least once, so callers can tell
    which uploaded artifacts were embedded inline versus appended as
    additional evidence.
    """
    by_filename: dict[str, MediaArtifact] = {}
    by_basename: dict[str, MediaArtifact] = {}
    for artifact in resolved:
        filename = artifact.get("filename", "")
        if not filename:
            continue
        by_filename.setdefault(filename, artifact)
        by_basename.setdefault(filename.rsplit("/", 1)[-1], artifact)

    referenced: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        url = match.group("url")
        # Absolute URLs never refer to a local artifact filename, so skip
        # them up front to avoid surprising rewrites of links the author
        # already pointed at an external resource.
        if "://" in url or url.startswith("//"):
            return match.group(0)
        artifact = (
            by_filename.get(url)
            or by_basename.get(url)
            or by_filename.get(url.rsplit("/", 1)[-1])
            or by_basename.get(url.rsplit("/", 1)[-1])
        )
        if artifact is None:
            return match.group(0)
        download_url = artifact.get("download_url", "")
        if not download_url:
            return match.group(0)
        referenced.add(artifact.get("filename", ""))
        bang = match.group("bang")
        text = match.group("text")
        title = match.group("title") or ""
        return f"{bang}[{text}]({download_url}{title})"

    substituted = _MARKDOWN_LINK_RE.sub(_replace, report)
    return substituted, referenced


def _build_skill_section(result: VerificationResult) -> str:
    """Render a single skill's section for the consolidated report body."""
    skill = result.get("skill", "")
    description = result.get("description", "").strip()
    report = result.get("report", "").strip()
    error = result.get("error", "").strip()
    session_link = result.get("session_link", "").strip()

    heading = f"### `{skill}`" if skill else "### verification skill"
    lines = [heading]
    if description:
        lines.append(f"_{description}_")
    if error:
        lines.append(f"❌ The verification run errored: {error}")
    elif report:
        lines.append(report)
    else:
        lines.append("The skill produced no `verification_report.md` artifact.")

    # Append any media artifacts the skill uploaded but did not reference
    # via a Markdown link so the evidence still reaches the reviewer.
    artifacts = result.get("artifacts") or []
    unreferenced = [
        artifact
        for artifact in artifacts
        if artifact.get("download_url")
        and artifact.get("download_url", "") not in report
    ]
    if unreferenced:
        lines.append("**Additional artifacts:**")
        for artifact in unreferenced:
            lines.append(_format_media_embed(artifact))

    if session_link:
        lines.append(f"_Oz run: [view on Warp]({session_link})_")
    return "\n\n".join(section for section in lines if section)


def build_consolidated_report(
    *,
    pr_number: int,
    requester: str,
    skill_filter: str,
    workflow_run_url: str,
    skills_considered: list[dict[str, str]],
    results: list[VerificationResult],
) -> str:
    """Assemble the final comment body posted back to the PR."""
    header_lines = [f"## `/oz-verify` report for PR #{pr_number}"]
    requester_clean = (requester or "").strip().lstrip("@")
    if requester_clean:
        header_lines.append(f"Requested by @{requester_clean}.")
    if skill_filter:
        header_lines.append(
            f"Discovery was narrowed by `skill_filter` to `{skill_filter}`."
        )
    if not skills_considered:
        header_lines.append(
            "No verification skills were found. Authors can opt in by "
            "declaring `verification: true` in a skill's frontmatter."
        )
    else:
        names = ", ".join(f"`{entry['name']}`" for entry in skills_considered)
        count = len(skills_considered)
        header_lines.append(
            f"Ran {count} verification skill{'s' if count != 1 else ''}: {names}."
        )

    sections = [_build_skill_section(result) for result in results]
    footer_lines = []
    if workflow_run_url:
        footer_lines.append(f"Workflow run: [view logs]({workflow_run_url}).")
    footer_lines.append(POWERED_BY_SUFFIX)

    blocks = ["\n\n".join(header_lines)]
    if sections:
        blocks.append("\n\n---\n\n".join(sections))
    blocks.append("\n\n".join(footer_lines))
    return "\n\n".join(blocks).strip() + "\n"


def _acknowledge_trigger_comment(pr: PullRequest, comment_id: int) -> None:
    """Leave an 👀 reaction on the triggering comment, best-effort."""
    try:
        pr.get_issue_comment(comment_id).create_reaction("eyes")
    except Exception:
        logger.exception(
            "Failed to acknowledge trigger comment %s on PR #%s",
            comment_id,
            pr.number,
        )


def _workflow_run_url() -> str:
    server = optional_env("GITHUB_SERVER_URL") or "https://github.com"
    repository = optional_env("GITHUB_REPOSITORY")
    run_id = optional_env("GITHUB_RUN_ID")
    if not repository or not run_id:
        return ""
    return f"{server}/{repository}/actions/runs/{run_id}"


def _build_skill_prompt(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    head_branch: str,
    skill: dict[str, str],
    requester: str,
    workflow_run_url: str,
) -> str:
    skill_name = skill["name"]
    description = skill.get("description") or ""
    description_line = (
        f"The skill's description is: {description}"
        if description
        else "Apply the skill's SKILL.md instructions exactly."
    )
    requester_clean = (requester or "").strip().lstrip("@")
    requester_line = (
        f"Requested by @{requester_clean} via `/oz-verify`."
        if requester_clean
        else "Triggered by `/oz-verify`."
    )
    run_url_line = (
        f"Workflow run URL: {workflow_run_url}"
        if workflow_run_url
        else "No workflow run URL was provided."
    )
    return dedent(
        f"""
        You are running the `{skill_name}` verification skill against pull request
        #{pr_number} in {owner}/{repo}. {requester_line}

        The PR is already checked out locally on branch `{head_branch}` at HEAD, so
        `git`, `gh`, and the repository filesystem are all available. {description_line}

        Verification Contract:
        1. Follow the skill's SKILL.md instructions end-to-end against the PR HEAD.
        2. Verification is READ-ONLY for tracked files: do not stage files, create
           commits, or push any branch. You may write untracked screenshot/video
           evidence under `verification_artifacts/{skill_name}/` (or any other
           working-tree path) while the run is in progress.
        3. Write a concise markdown report to `{VERIFICATION_REPORT_FILENAME}` at
           the repository root. The report should:
           - Start with a one-line status (`✅ Passed`, `❌ Failed`, or `⚠️ Errored`).
           - Summarize what was verified and any output worth linking.
           - Reference each uploaded screenshot/video artifact with a standard
             Markdown image or link whose URL is the uploaded artifact's filename,
             e.g. `![login success](login-success.png)` or `[demo](demo.mp4)`. The
             workflow rewrites each such URL with a signed download URL drawn
             from the Oz run's artifacts, so do NOT construct image or video
             URLs yourself.
        4. For every screenshot or short video you want embedded in the PR comment,
           upload it as an artifact via:
               oz-dev artifact upload <path>
           The subcommand is `artifact` (singular); do not use `artifacts`.
           Supported media types are PNG, JPEG, GIF, WebP, MP4, MOV, and WebM.
           After uploading each file, reference it from the report using a
           standard Markdown image or link whose URL is the filename you passed
           to `oz-dev artifact upload` (e.g. `![alt text](screenshot.png)`).
        5. Upload the report itself as an artifact so the workflow can fetch it:
               oz-dev artifact upload {VERIFICATION_REPORT_FILENAME}
        6. DO NOT post the report back to the PR yourself. The workflow consolidates
           reports across every verification skill into a single comment and posts
           it once all skills have run. {run_url_line}
        """
    ).strip()


def _run_single_skill(
    *,
    skill: dict[str, str],
    owner: str,
    repo: str,
    pr_number: int,
    head_branch: str,
    requester: str,
    workflow_run_url: str,
    oz_client: OzAPI,
    progress: WorkflowProgressComment,
) -> VerificationResult:
    """Execute a single verification skill and collect its outputs."""
    skill_name = skill["name"]
    prompt = _build_skill_prompt(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        head_branch=head_branch,
        skill=skill,
        requester=requester,
        workflow_run_url=workflow_run_url,
    )
    config = build_agent_config(
        config_name=f"oz-verify-{skill_name}",
        workspace=workspace(),
    )
    try:
        run = run_agent(
            prompt=prompt,
            skill_name=skill_name,
            title=f"Oz verify {skill_name} on PR #{pr_number}",
            config=config,
            on_poll=lambda current_run: record_run_session_link(progress, current_run),
        )
    except Exception as exc:
        logger.exception(
            "Verification skill %s failed for PR #%s", skill_name, pr_number
        )
        return {
            "skill": skill_name,
            "description": skill.get("description", ""),
            "report": "",
            "artifacts": [],
            "error": str(exc) or type(exc).__name__,
            "session_link": "",
        }

    try:
        report = poll_for_text_artifact(
            run.run_id, filename=VERIFICATION_REPORT_FILENAME
        )
    except Exception:
        logger.exception(
            "Failed to fetch %s from run %s for skill %s",
            VERIFICATION_REPORT_FILENAME,
            run.run_id,
            skill_name,
        )
        report = ""

    media_artifacts = collect_media_artifacts(run)
    resolved_artifacts = resolve_media_download_urls(oz_client, media_artifacts)
    substituted_report, _referenced = substitute_artifact_links(
        report, resolved_artifacts
    )
    return {
        "skill": skill_name,
        "description": skill.get("description", ""),
        "report": substituted_report,
        "artifacts": resolved_artifacts,
        "error": "",
        "session_link": str(getattr(run, "session_link", "") or ""),
    }


def main() -> None:
    owner, repo = repo_parts()
    pr_number = int(require_env("PR_NUMBER"))
    requester = optional_env("REQUESTER")
    skill_filter = optional_env("SKILL_FILTER")
    comment_id_raw = optional_env("COMMENT_ID")
    workflow_run_url = _workflow_run_url()
    workspace_root = workspace()

    with closing(Github(auth=Auth.Token(require_env("GH_TOKEN")))) as client:
        github = client.get_repo(repo_slug())
        pr = github.get_pull(pr_number)
        if pr.state != "open":
            return
        if comment_id_raw and comment_id_raw.isdigit():
            _acknowledge_trigger_comment(pr, int(comment_id_raw))

        progress = WorkflowProgressComment(
            github,
            owner,
            repo,
            pr_number,
            workflow="oz-verify",
            requester_login=requester,
        )
        skills = discover_verification_skills(
            workspace_root, skill_filter=skill_filter
        )
        skill_names = [entry["name"] for entry in skills]
        if skill_filter:
            progress.start(
                "I'm running the `"
                + skill_filter
                + "` verification skill on this PR."
            )
        elif skill_names:
            progress.start(
                f"I'm running {len(skill_names)} verification skill(s) on this PR: "
                + ", ".join(f"`{name}`" for name in skill_names)
                + "."
            )
        else:
            progress.start(
                "I looked for verification skills in `.agents/skills/` but "
                "found none with `verification: true` frontmatter. "
                "See the `/oz-verify` docs for the opt-in convention."
            )

        try:
            results: list[VerificationResult] = []
            if skills:
                oz_client = build_oz_client()
                for skill in skills:
                    results.append(
                        _run_single_skill(
                            skill=skill,
                            owner=owner,
                            repo=repo,
                            pr_number=pr_number,
                            head_branch=pr.head.ref,
                            requester=requester,
                            workflow_run_url=workflow_run_url,
                            oz_client=oz_client,
                            progress=progress,
                        )
                    )

            report_body = build_consolidated_report(
                pr_number=pr_number,
                requester=requester,
                skill_filter=skill_filter,
                workflow_run_url=workflow_run_url,
                skills_considered=skills,
                results=results,
            )
            posted = pr.create_issue_comment(report_body)
            completion_sections = [
                f"I posted a [verification report]({posted.html_url}) on this PR.",
            ]
            if not skills:
                completion_sections.append(
                    "No verification skills were discovered — add a "
                    "`.agents/skills/verify-*/SKILL.md` with `verification: true` "
                    "in its frontmatter to enable end-to-end verification."
                )
            errored = [r for r in results if r.get("error")]
            if errored:
                failed_names = ", ".join(f"`{r['skill']}`" for r in errored)
                completion_sections.append(
                    f"The following skill runs errored: {failed_names}. "
                    "See the workflow run logs for details."
                )
            completion_sections.append(
                build_next_steps_section(
                    [
                        "Review the verification report above for pass/fail status and embedded evidence.",
                        "Re-run `/oz-verify` (optionally with a single skill name) to verify again.",
                    ]
                )
            )
            progress.complete("\n\n".join(s for s in completion_sections if s))
        except Exception:
            progress.report_error()
            raise


if __name__ == "__main__":
    main()
