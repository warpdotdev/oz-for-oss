from __future__ import annotations

from textwrap import dedent
from oz_workflows.artifacts import load_pr_metadata_artifact

from oz_workflows.env import optional_env, repo_parts, workspace
from oz_workflows.oz_client import build_agent_config, run_agent
from oz_workflows.repo_local import (
    WriteSurfaceViolation,
    maybe_push_update_branch,
)


UPDATE_BRANCH = "oz-agent/update-triage"
ALLOWED_PREFIXES: tuple[str, ...] = (
    ".agents/skills/triage-issue-local/",
    ".github/issue-triage/",
)


def main() -> None:
    owner, repo = repo_parts()
    days = optional_env("LOOKBACK_DAYS") or "7"

    prompt = dedent(
        f"""
        Update the repo-local triage companion skill for repository {owner}/{repo}.

        Use the repository's local `update-triage` skill as the base workflow.

        Cloud Workflow Requirements:
        - You are running in a cloud environment with the repository already checked out.
        - Run the feedback aggregation script with a {days}-day lookback window.
        - The aggregated feedback includes maintainer label changes, re-opens, and follow-up comments on recently triaged issues. Closed-as-duplicate signals are handled by a separate `update-dedupe` loop and are NOT included here.
        - Route feedback into `.agents/skills/triage-issue-local/SKILL.md`. When a label-taxonomy change is warranted, `.github/issue-triage/config.json` may also be updated.
        - Do NOT edit the core skill at `.agents/skills/triage-issue/SKILL.md`. It is the cross-repo contract and is read-only from this loop.
        - Do NOT edit any file under `.github/scripts/`. The prompt-construction layer is also read-only from this loop.
        - The allowed write surface is strictly `.agents/skills/triage-issue-local/` and `.github/issue-triage/`.
        - If you produce changes, write `pr-metadata.json` at the repository root containing a JSON object with these required fields:
          - `branch_name`: the branch you committed to (use `{UPDATE_BRANCH}` exactly).
          - `pr_title`: a conventional-commit-style PR title derived from the actual updates.
          - `pr_summary`: the full markdown PR body summarizing the evidence-backed companion/config changes.
        - After writing `pr-metadata.json`, upload it as an artifact via `oz artifact upload pr-metadata.json` (or `oz-preview artifact upload pr-metadata.json` if the `oz` CLI is not available). The subcommand is `artifact` (singular) on both CLIs; do not use `artifacts`.
        - If you produce changes, commit them to a local branch named `{UPDATE_BRANCH}` but do NOT push the branch yourself. The Python entrypoint will run a write-surface guard and push only when the guard passes.
        - If no companion update is warranted based on the feedback, do not create a commit. Leave the working tree clean.
        """
    ).strip()

    config = build_agent_config(
        config_name="update-triage",
        workspace=workspace(),
    )
    run = run_agent(
        prompt=prompt,
        skill_name="update-triage",
        title="Update triage companion skill from maintainer feedback",
        config=config,
    )

    pr_title = "chore: update triage companion skill from maintainer feedback"
    pr_body = (
        "Automated update from the `update-triage` self-improvement loop.\n\n"
        "This PR proposes evidence-backed edits to "
        "`.agents/skills/triage-issue-local/SKILL.md` "
        "(and, when warranted, `.github/issue-triage/config.json`) "
        "based on recent maintainer signals on triaged issues."
    )
    maybe_push_update_branch(
        workspace(),
        UPDATE_BRANCH,
        allowed_prefixes=list(ALLOWED_PREFIXES),
        loop_name="update-triage",
        pr_title=pr_title,
        pr_body=pr_body,
        metadata_supplier=lambda: load_pr_metadata_artifact(run.run_id),
    )


if __name__ == "__main__":
    try:
        main()
    except WriteSurfaceViolation as exc:
        raise SystemExit(str(exc))
