from __future__ import annotations

from contextlib import closing
from pathlib import Path
from textwrap import dedent
from typing import Any, Mapping, TypedDict

from github import Auth, Github
from github.Repository import Repository

from oz_workflows.artifacts import load_run_artifact, poll_for_artifact
from oz_workflows.env import load_event, repo_parts, repo_slug, require_env, workspace
from oz_workflows.helpers import (
    WorkflowProgressComment,
    is_automation_user,
    record_run_session_link,
)
from oz_workflows.oz_client import build_agent_config, run_agent
from oz_workflows.verification import (
    discover_verification_skills,
    format_verification_skills_for_prompt,
    list_downloadable_verification_artifacts,
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

    Mirrors the cleanup branch in :func:`main`: replaces the progress
    comment body with the rendered report and (when present) any
    downloadable verification artifacts the agent uploaded. The cron
    poller passes through the same ``WorkflowProgressComment`` shape
    that ``main`` constructs so the rendered comment metadata is
    identical between the GitHub Actions and Vercel paths.

    *progress* is the reconstructed :class:`WorkflowProgressComment` the
    Vercel cron handler hands in so the final ``replace_body`` call
    lands on the comment posted at dispatch time. Callers that omit it
    fall back to constructing a fresh instance, which keeps the legacy
    GHA runtime contract.
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


def main() -> None:
    owner, repo = repo_parts()
    event = load_event()
    comment = event.get("comment") or {}
    if is_automation_user(comment.get("user")):
        return
    issue = event.get("issue") or {}
    if not issue.get("pull_request"):
        return

    trigger_comment_id = int(comment["id"])
    requester = (comment.get("user") or {}).get("login") or ""
    pr_number = int(issue["number"])

    with closing(Github(auth=Auth.Token(require_env("GH_TOKEN")))) as client:
        # The organization-membership gate was removed: the bot now
        # runs ``/oz-verify`` for every human-authored mention. The
        # ``is_automation_user`` check above already skips bot-authored
        # commands, which is the only filter we still apply.
        github = client.get_repo(repo_slug())
        pr = github.get_pull(pr_number)
        pr.get_issue_comment(trigger_comment_id).create_reaction("eyes")

        workspace_path = workspace()
        verification_skills = discover_verification_skills(workspace_path)
        progress = WorkflowProgressComment(
            github,
            owner,
            repo,
            pr_number,
            workflow=WORKFLOW_NAME,
            event_payload=event,
            requester_login=requester,
        )
        progress.start(
            "I'm running `/oz-verify` for this pull request using the repository's verification-enabled skills."
        )

        if not verification_skills:
            progress.complete(
                "I couldn't run `/oz-verify` because this repository does not currently expose any skills with `metadata.verification: true` under `.agents/skills/`."
            )
            return

        context = gather_verify_context(
            github,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            trigger_comment_id=trigger_comment_id,
            requester=requester,
            workspace_path=workspace_path,
        )
        prompt = build_verification_prompt(
            owner=context["owner"],
            repo=context["repo"],
            pr_number=context["pr_number"],
            base_branch=context["base_branch"],
            head_branch=context["head_branch"],
            trigger_comment_id=context["trigger_comment_id"],
            requester=context["requester"],
            verification_skills_text=context["verification_skills_text"],
        )

        config = build_agent_config(
            config_name=WORKFLOW_NAME,
            workspace=workspace(),
        )
        try:
            run = run_agent(
                prompt=prompt,
                skill_name=VERIFY_PR_SKILL,
                title=f"Verify PR #{pr_number}",
                config=config,
                on_poll=lambda current_run: record_run_session_link(progress, current_run),
            )
            report = poll_for_artifact(run.run_id, filename=VERIFICATION_REPORT_FILENAME)
            artifacts = list_downloadable_verification_artifacts(
                run,
                exclude_filenames={VERIFICATION_REPORT_FILENAME},
            )
            apply_verification_result(
                github,
                context=context,
                run=run,
                result=report,
                artifacts=artifacts,
            )
        except Exception:
            progress.report_error()
            raise


if __name__ == "__main__":
    main()
