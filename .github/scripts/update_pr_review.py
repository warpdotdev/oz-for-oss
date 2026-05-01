from __future__ import annotations

from textwrap import dedent
from oz_workflows.artifacts import load_pr_metadata_artifact

from oz_workflows.env import optional_env, repo_parts, workspace
from oz_workflows.oz_client import build_agent_config, run_agent
from oz_workflows.repo_local import (
    WriteSurfaceViolation,
    maybe_push_update_branch,
)


UPDATE_BRANCH = "oz-agent/update-pr-review"
# Write surface is strictly the review companions. The issue-triage config
# is owned by ``update-triage`` (and the triage label taxonomy is a triage
# signal, not a PR-review signal). Letting this loop edit it would create
# dual-ownership and could silently mutate triage config from misclassified
# PR feedback.
ALLOWED_PREFIXES: tuple[str, ...] = (
    ".agents/skills/review-pr-local/",
    ".agents/skills/review-spec-local/",
)


def main() -> None:
    owner, repo = repo_parts()
    days = optional_env("LOOKBACK_DAYS") or "7"

    prompt = dedent(
        f"""
        Update the repo-local PR review companion skills for repository {owner}/{repo}.

        Use the repository's local `update-pr-review` skill as the base workflow.

        Cloud Workflow Requirements:
        - You are running in a cloud environment with the repository already checked out.
        - Run the feedback aggregation script with a {days}-day lookback window.
        - The aggregated feedback includes a `review_type` field per PR: `"code"` or `"spec"`.
        - Route feedback from `"code"` PRs to `.agents/skills/review-pr-local/SKILL.md` and feedback from `"spec"` PRs to `.agents/skills/review-spec-local/SKILL.md`.
        - Do NOT edit the core skills at `.agents/skills/review-pr/SKILL.md` or `.agents/skills/review-spec/SKILL.md`. They are the cross-repo contract and are read-only from this loop.
        - Do NOT edit any file under `.github/scripts/` or under `.github/issue-triage/`. The prompt-construction layer and the triage label taxonomy are out of scope for this loop.
        - The allowed write surface is strictly `.agents/skills/review-pr-local/` and `.agents/skills/review-spec-local/`.
        - Update each companion skill independently based on its category of feedback. Skip a companion if its category has no actionable feedback.
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
        config_name="update-pr-review",
        workspace=workspace(),
    )
    run = run_agent(
        prompt=prompt,
        skill_name="update-pr-review",
        title="Update PR review companion skills from feedback",
        config=config,
    )

    pr_title = "chore: update PR review companion skills from feedback"
    pr_body = (
        "Automated update from the `update-pr-review` self-improvement loop.\n\n"
        "This PR proposes evidence-backed edits to "
        "`.agents/skills/review-pr-local/SKILL.md` and/or "
        "`.agents/skills/review-spec-local/SKILL.md` based on recent "
        "human PR review feedback."
    )
    maybe_push_update_branch(
        workspace(),
        UPDATE_BRANCH,
        allowed_prefixes=list(ALLOWED_PREFIXES),
        loop_name="update-pr-review",
        pr_title=pr_title,
        pr_body=pr_body,
        metadata_supplier=lambda: load_pr_metadata_artifact(run.run_id),
    )


if __name__ == "__main__":
    try:
        main()
    except WriteSurfaceViolation as exc:
        # Fail loud when the loop touched disallowed files, so CI surfaces
        # the problem rather than pushing a PR that regresses the core
        # skill contract or the workflow scripts.
        raise SystemExit(str(exc))
