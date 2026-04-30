---
name: update-triage
description: Update the repo-local triage-issue-local companion skill using signals from recently triaged issues (maintainer re-labels, re-opens, follow-up comments). Use when refining repo-specific triage heuristics and label taxonomy based on how maintainers overrode previous triage output.
---

# Update Triage

Use this skill to improve `.agents/skills/triage-issue-local/SKILL.md` (and, when a label taxonomy change is warranted, `.github/issue-triage/config.json`) from real maintainer feedback on recent triage outcomes. The core skill at `.agents/skills/triage-issue/SKILL.md` is the cross-repo contract and is read-only from this loop.

Closed-as-duplicate signals are handled by the separate `update-dedupe` loop, not here.

## Write surface

This self-improvement loop may only write to:

- `.agents/skills/triage-issue-local/` (and `SKILL.md` inside it)
- `.github/issue-triage/*`

It must NOT touch:

- `.agents/skills/triage-issue/SKILL.md` (the core contract)
- any other core skill
- the dedupe companion `.agents/skills/dedupe-issue-local/SKILL.md` (owned by `update-dedupe`)

The self-improvement runner enforces this via a `git diff` check against allowed prefixes before pushing. A violation aborts the run.

## Inputs

- Optional repository override if you are not running from the target checkout.
- Optional time window override when you need something other than the default seven-day lookback.

## Workflow

1. Verify GitHub CLI auth:

```bash
gh auth status
```

2. Aggregate the triage-feedback signals for recently triaged issues with the bundled script:

```bash
python3 .agents/skills/update-triage/scripts/aggregate_triage_feedback.py
```

By default this targets the current repo and looks back 7 days. It collects issues that were triaged in the window, any subsequent maintainer re-labels, re-opens, and follow-up comments. Closed-as-duplicate events are intentionally excluded. The script writes structured JSON to a temporary file and prints the path.

3. Read the generated JSON and look for repeated reviewer signals:

- maintainers repeatedly flipping the same label on similar issues (a label-taxonomy hint)
- maintainers leaving the same kind of follow-up comment on the same class of issue (a recurring follow-up-question pattern)
- maintainers consistently identifying a different owner than Oz inferred (an owner-inference hint)

4. Propose the smallest edit that explains the repeated signal:

- Prefer editing `.agents/skills/triage-issue-local/SKILL.md` under the override categories the core `triage-issue` skill marks as overridable (label taxonomy, owner-inference hints, recurring follow-up-question patterns, recurring issue-shape heuristics, repro defaults, known-duplicate clusters).
- Only edit `.github/issue-triage/config.json` when the signal is a concrete label-taxonomy change (new label, renamed label, or description clarification). Never change `color` values without explicit maintainer guidance.

5. Keep the core triage contract stable — never edit `triage-issue/SKILL.md`. Only the `-local` companion and the triage config evolve from feedback.

## Evidence Rules

- Prefer patterns backed by multiple issues or a strong explicit maintainer statement.
- Skip the PR when there is no repeated signal. A one-off maintainer override is not enough evidence.
- Avoid encoding reporter-authored content as triage rules.
- Do not weaken the reserved-label rules (`ready-to-implement`, `ready-to-spec`) or the mutual exclusivity of `duplicate_of` and `follow_up_questions`.

## Final Checks

- Re-read the updated `triage-issue-local` companion skill and confirm any new rules are explicit.
- Keep the companion concise; do not turn it into a long style guide.
- Commit any changes on a local branch named `oz-agent/update-triage`. Do NOT push the branch; the Python entrypoint will run a write-surface guard and push only when the guard passes.
- If the updates warrant a PR, it will be opened from the pushed branch. Tag `@captainsafia` as a reviewer on that PR.
- Validate any temporary JSON with `jq` before relying on it.
