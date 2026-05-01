from __future__ import annotations

from textwrap import dedent
from oz_workflows.artifacts import load_pr_metadata_artifact

from oz_workflows.env import optional_env, repo_parts, workspace
from oz_workflows.oz_client import build_agent_config, run_agent
from oz_workflows.repo_local import (
    WriteSurfaceViolation,
    maybe_push_update_branch,
)


UPDATE_BRANCH = "oz-agent/update-dedupe"
ALLOWED_PREFIXES: tuple[str, ...] = (
    ".agents/skills/dedupe-issue-local/",
)


def main() -> None:
    owner, repo = repo_parts()
    days = optional_env("LOOKBACK_DAYS") or "7"

    prompt = dedent(
        f"""
        Update the repo-local dedupe companion skill for repository {owner}/{repo}.

        Use the repository's local `update-dedupe` skill as the base workflow.

        Cloud Workflow Requirements:
        - You are running in a cloud environment with the repository already checked out.
        - Run the feedback aggregation script with a {days}-day lookback window.
        - The aggregated feedback is restricted to closed-as-duplicate signals. Other triage signals are handled by the separate `update-triage` loop.
        - Route feedback into `.agents/skills/dedupe-issue-local/SKILL.md` only.
        - Do NOT edit the core skill at `.agents/skills/dedupe-issue/SKILL.md`. It is the cross-repo contract and is read-only from this loop.
        - Do NOT edit `.agents/skills/triage-issue-local/SKILL.md` or any file under `.github/scripts/`.
        - The allowed write surface is strictly `.agents/skills/dedupe-issue-local/`.
        - If you produce changes, write `pr-metadata.json` at the repository root containing a JSON object with these required fields:
          - `branch_name`: the branch you committed to (use `{UPDATE_BRANCH}` exactly).
          - `pr_title`: a conventional-commit-style PR title derived from the actual updates.
          - `pr_summary`: the full markdown PR body summarizing the evidence-backed companion-skill changes.
        - After writing `pr-metadata.json`, upload it as an artifact via `oz artifact upload pr-metadata.json` (or `oz-preview artifact upload pr-metadata.json` if the `oz` CLI is not available). The subcommand is `artifact` (singular) on both CLIs; do not use `artifacts`.
        - If you produce changes, commit them to a local branch named `{UPDATE_BRANCH}` but do NOT push the branch yourself. The Python entrypoint will run a write-surface guard and push only when the guard passes.
        - If no companion update is warranted based on the feedback, do not create a commit. Leave the working tree clean.
        """
    ).strip()

    config = build_agent_config(
        config_name="update-dedupe",
        workspace=workspace(),
    )
    run = run_agent(
        prompt=prompt,
        skill_name="update-dedupe",
        title="Update dedupe companion skill from closed-as-duplicate signals",
        config=config,
    )

    pr_title = "chore: update dedupe companion skill from closed-as-duplicate signals"
    pr_body = (
        "Automated update from the `update-dedupe` self-improvement loop.\n\n"
        "This PR proposes evidence-backed edits to "
        "`.agents/skills/dedupe-issue-local/SKILL.md` based on recent "
        "closed-as-duplicate events and their canonical-issue links."
    )
    maybe_push_update_branch(
        workspace(),
        UPDATE_BRANCH,
        allowed_prefixes=list(ALLOWED_PREFIXES),
        loop_name="update-dedupe",
        pr_title=pr_title,
        pr_body=pr_body,
        metadata_supplier=lambda: load_pr_metadata_artifact(run.run_id),
    )


if __name__ == "__main__":
    try:
        main()
    except WriteSurfaceViolation as exc:
        raise SystemExit(str(exc))
