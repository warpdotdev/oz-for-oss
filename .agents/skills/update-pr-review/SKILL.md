---
name: update-pr-review
description: Update the repo-local review-pr-local and review-spec-local companion skills using human feedback left on pull request conversations. Use when aggregating replies to agent-authored PR review comments, incorporating broader human review comments, extracting repeated reviewer feedback, and refining .agents/skills/review-pr-local/SKILL.md and .agents/skills/review-spec-local/SKILL.md with evidence-backed adjustments.
---

# Update PR Review

Use this skill to improve the repo-local review companions `.agents/skills/review-pr-local/SKILL.md` and `.agents/skills/review-spec-local/SKILL.md` from real reviewer feedback. The core skills at `.agents/skills/review-pr/SKILL.md` and `.agents/skills/review-spec/SKILL.md` are the cross-repo contract and are read-only from this loop.

The repository uses two separate review skills: `review-pr` for code pull requests and `review-spec` for spec-only pull requests (PRs where every changed file lives under `specs/`). Feedback from each category of PR should be routed to the corresponding repo-local companion.

## Write surface

This self-improvement loop may only write to:

- `.agents/skills/review-pr-local/` (and `SKILL.md` inside it)
- `.agents/skills/review-spec-local/` (and `SKILL.md` inside it)

It must NOT touch:

- `.agents/skills/review-pr/SKILL.md` (the core contract)
- `.agents/skills/review-spec/SKILL.md` (the core contract)
- any file under `.github/issue-triage/` (that taxonomy is owned by the `update-triage` loop)
- any other core skill

The self-improvement runner enforces this via a `git diff` check against allowed prefixes before pushing. A violation aborts the run.

## Inputs

- Optional repository override if you are not running from the target checkout.
- Optional time window override when you need something other than the default seven-day lookback.
- Optional agent-login override if the review comments were not authored by the default bot identities.

## Workflow

1. Verify GitHub CLI auth:

```bash
gh auth status
```

2. Aggregate the feedback for pull requests updated over the last week with the bundled script:

```bash
python3 .agents/skills/update-pr-review/scripts/aggregate_review_feedback.py
```

By default this targets the current repo, looks back 7 days, and analyzes review comments authored by the bot identities used by the PR review workflow (`warp-dev-github-integration[bot]`). It also collects broader human review comments from those PRs so the skill can learn from reviewer norms even when they were not replying directly to the bot. The script writes structured JSON to a temporary file and prints the temp-file path. Treat that file as scratch state for this skill, not as a user-facing deliverable or final output. If you need a repository other than the current checkout, pass `--repo owner/name`. If you need a different author identity, pass `--agent-login <login>` one or more times.

Each pull request in the output includes a `review_type` field that is either `"spec"` (all changed files under `specs/`) or `"code"` (any non-spec file changed). Use this field to route feedback to the correct skill.

3. Read the generated JSON and look for repeated reviewer signals, especially:

- replies that say the agent's feedback was wrong, invalid, not applicable, or based on a bad assumption
- signals that the agent had the right instinct but the wrong severity, scope, line targeting, or proposed fix
- feedback that the comment was not actionable enough, including requests for clearer concrete changes
- recurring cases where humans override the bot because repository or product context changes the right call
- review patterns from human-only threads that show what experienced reviewers in this repo consistently care about
- explicit reviewer guidance about what belongs inline, what belongs in the summary, and when the bot should stay uncertain

4. Partition the feedback by `review_type`:

- Feedback from `"code"` PRs applies to `.agents/skills/review-pr-local/SKILL.md`.
- Feedback from `"spec"` PRs applies to `.agents/skills/review-spec-local/SKILL.md`.
- Update each companion skill independently with the smallest rule change that explains the feedback for that category.
- If feedback for one category is empty, skip that companion.

5. Keep the core review contract stable — never edit `review-pr/SKILL.md` or `review-spec/SKILL.md`. Only the `-local` companions evolve from feedback.

## Evidence Rules

- Prefer patterns backed by multiple threads or a strong explicit maintainer statement.
- Do not weaken correctness, security, or data-loss checks because of a single disagreement.
- Separate feedback about review quality from feedback about repository-specific preferences.
- Avoid encoding one-off reviewer preferences as universal rules.
- If the feedback points to missing repository context, add that context only if it improves review precision.
- Do not mix code-review feedback into the spec skill, or spec-review feedback into the code skill.

## Intermediary State

The script builds structured JSON that captures:

- pull request metadata for the recent PR window, including `review_type` classification
- agent-authored review comments that received human replies
- human-authored review comments from the same PRs, even when they were not replying to the bot
- thread metadata like file path, line, resolution, and outdated state
- normalized agent-comment fields such as severity label and whether a suggestion block was present
- the full set of human replies for each agent comment
- top-level PR issue comments for broader review context

Use that temporary data as evidence when refining the skills, then remove it before finishing if you wrote it to disk explicitly.

## Final Checks

- Re-read the updated `review-pr-local` and/or `review-spec-local` companion skills and confirm any new rules are explicit.
- Keep each companion concise; do not turn them into long style guides.
- Commit any changes on a local branch named `oz-agent/update-pr-review`. Do NOT push the branch; the Python entrypoint will run a write-surface guard and push only when the guard passes.
- If the updates warrant a PR, it will be opened from the pushed branch. Tag `@captainsafia` as a reviewer on that PR.
- Validate any temporary JSON with `jq` before relying on it.
