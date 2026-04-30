from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any, Mapping, TypedDict

from github.Repository import Repository

from oz.helpers import WorkflowProgressComment
from oz.verification import (
    discover_verification_skills,
    discover_verification_skills_from_repo,
    format_verification_skills_for_prompt,
    render_verification_comment,
)

WORKFLOW_NAME = "verify-pr-comment"
FETCH_CONTEXT_SCRIPT = ".agents/skills/implement-specs/scripts/fetch_github_context.py"
VERIFY_PR_SKILL = "verify-pr"
VERIFICATION_REPORT_FILENAME = "verification_report.json"


class VerifyContext(TypedDict):
    """Serializable context for a verify-pr-comment dispatch.

    The control plane stores this dict verbatim in ``RunState.payload_subset``
    so the cron poller can apply the result without re-fetching anything
    from GitHub.
    """

    owner: str
    repo: str
    pr_number: int
    base_branch: str
    head_branch: str
    trigger_comment_id: int
    requester: str
    verification_skills_text: str


def gather_verify_context(
    github: Repository,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    trigger_comment_id: int,
    requester: str,
    workspace_path: Path,
) -> VerifyContext:
    """Gather the GitHub-side context needed to dispatch a verify run.

    Returns a serializable :class:`VerifyContext`. The webhook handler
    saves the dict on ``RunState.payload_subset`` and the cron poller
    applies the result without re-fetching from GitHub.
    """
    pr = github.get_pull(pr_number)
    verification_skills = discover_verification_skills_from_repo(github)
    if not verification_skills:
        verification_skills = discover_verification_skills(workspace_path)
    verification_skills_text = format_verification_skills_for_prompt(
        verification_skills,
        workspace_root=workspace_path,
    )
    return VerifyContext(
        owner=owner,
        repo=repo,
        pr_number=int(pr_number),
        base_branch=str(pr.base.ref),
        head_branch=str(pr.head.ref),
        trigger_comment_id=int(trigger_comment_id),
        requester=str(requester or ""),
        verification_skills_text=verification_skills_text,
    )


def apply_verification_result(
    github: Repository,
    *,
    context: Mapping[str, Any],
    run: Any,
    result: Mapping[str, Any],
    artifacts: list[Mapping[str, Any]] | None = None,
    progress: WorkflowProgressComment | None = None,
) -> None:
    """Apply a completed verification report back to GitHub.

    Replaces the progress comment body with the rendered report and
    (when present) any downloadable verification artifacts the agent
    uploaded. The cron poller passes through the
    ``WorkflowProgressComment`` posted at dispatch time so the final
    comment metadata remains stable.

    *progress* is the reconstructed :class:`WorkflowProgressComment` the
    Vercel cron handler hands in so the final ``replace_body`` call
    lands on the comment posted at dispatch time. Callers that omit it
    fall back to constructing a fresh instance.
    """
    if progress is None:
        progress = WorkflowProgressComment(
            github,
            str(context["owner"]),
            str(context["repo"]),
            int(context["pr_number"]),
            workflow=WORKFLOW_NAME,
            requester_login=str(context.get("requester") or ""),
        )
    progress.replace_body(
        render_verification_comment(
            result,
            session_link=str(getattr(run, "session_link", "") or ""),
            artifacts=list(artifacts or []),
        )
    )


def build_verification_prompt(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    base_branch: str,
    head_branch: str,
    trigger_comment_id: int,
    requester: str,
    verification_skills_text: str,
) -> str:
    return dedent(
        f"""\
        Run pull request verification for pull request #{pr_number} in repository {owner}/{repo}.

        Pull Request Metadata:
        - Base branch: {base_branch}
        - Head branch: {head_branch}
        - Triggered by: PR conversation comment id={trigger_comment_id} from @{requester or 'unknown'}

        Discovered Verification Skills:
        {verification_skills_text}

        Fetching PR and Comment Content:
        - The PR body, conversation comments, review comments, and unified diff are NOT inlined in this prompt.
        - Fetch PR discussion on demand by running `python {FETCH_CONTEXT_SCRIPT} --repo {owner}/{repo} pr --number {pr_number}` from the repository root.
        - If you need the unified diff for this PR, run `python {FETCH_CONTEXT_SCRIPT} --repo {owner}/{repo} pr-diff --number {pr_number}` rather than reconstructing it yourself.
        - This script (and the filtering it applies) is the only supported way to read PR body or comment content during this run. Do not retrieve them via any other mechanism.

        Workflow Requirements:
        - Use the repository's local `verify-pr` skill as the base workflow.
        - Verify the code on branch `{head_branch}`. Fetch the branch and run your verification work against that branch rather than against the default branch.
        - Read and execute every discovered verification skill listed above. Do not silently skip a listed skill.
        - If a skill cannot be completed, record that clearly in the verification report.
        - If verification creates screenshots, images, videos, or other reviewer-useful files, upload them as artifacts via `oz artifact upload <path>` (or `oz-preview artifact upload <path>` if the `oz` CLI is not available).
        - Do not commit, push, edit the pull request, or post GitHub comments yourself.

        Report Output:
        - Write `verification_report.json` at the repository root with exactly this shape:
          {{
            "overall_status": "passed" | "failed" | "mixed",
            "summary": "markdown summary of the overall verification outcome",
            "skills": [
              {{
                "name": "skill name",
                "path": ".agents/skills/example/SKILL.md",
                "status": "passed" | "failed" | "mixed" | "skipped",
                "summary": "short reviewer-facing summary"
              }}
            ]
          }}
        - Include one `skills` entry for every discovered verification skill listed above.
        - Validate `verification_report.json` with `jq`.
        - Upload `verification_report.json` as an artifact via `oz artifact upload verification_report.json` (or `oz-preview artifact upload verification_report.json` if the `oz` CLI is not available).
        """
    ).strip()
