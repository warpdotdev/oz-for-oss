---
name: update-dedupe
description: Update the repo-local dedupe-issue-local companion skill using closed-as-duplicate signals. Use when maintainers repeatedly close issues as duplicates of the same canonical thread and that pattern should feed back into triage duplicate detection.
---

# Update Dedupe

Use this skill to improve `.agents/skills/dedupe-issue-local/SKILL.md` from real closed-as-duplicate signals. The core skill at `.agents/skills/dedupe-issue/SKILL.md` is the cross-repo contract and is read-only from this loop.

This loop is focused exclusively on closed-as-duplicate signals. Other maintainer signals (re-labels, re-opens, follow-up comments) are handled by the separate `update-triage` loop.

## Write surface

This self-improvement loop may only write to:

- `.agents/skills/dedupe-issue-local/` (and `SKILL.md` inside it)

It must NOT touch:

- `.agents/skills/dedupe-issue/SKILL.md` (the core contract)
- `.agents/skills/triage-issue-local/SKILL.md` (owned by `update-triage`)
- any other core skill

The self-improvement runner enforces this via a `git diff` check against allowed prefixes before pushing. A violation aborts the run.

## Inputs

- Optional repository override if you are not running from the target checkout.
- Optional time window override when you need something other than the default seven-day lookback.

## Workflow

1. Verify GitHub CLI auth:

```bash
gh auth status
```

2. Aggregate closed-as-duplicate signals for recently closed issues with the bundled script:

```bash
python3 .agents/skills/update-dedupe/scripts/aggregate_dedupe_feedback.py
```

By default this targets the current repo and looks back 7 days. It collects only issues GitHub itself recorded as closed with the *duplicate* close reason (`state_reason == "duplicate"`) and looks up the canonical issue each was closed against via the issue timeline's `marked_as_duplicate` event. Ad-hoc maintainer comments that merely mention another issue are intentionally ignored to avoid false positives. The script writes structured JSON to a temporary file and prints the path.

3. Read the generated JSON and look for repeated clusters:

- two or more different reporters filed similar issues that maintainers closed as duplicates of the same canonical thread
- an explicit maintainer statement that a class of issue should always be treated as a duplicate of a canonical thread

4. Propose the smallest edit that explains the cluster. Add or update a bullet under the "Known-duplicate clusters" section of `.agents/skills/dedupe-issue-local/SKILL.md` summarizing the canonical thread and the distinguishing signals maintainers use to identify duplicates.

5. Keep the core dedupe contract stable — never edit `dedupe-issue/SKILL.md`, never weaken the 2-candidate minimum, never change the similarity thresholds or the output shape.

## Evidence Rules

- Only encode a cluster when at least two independent closed-as-duplicate events point at the same canonical issue, or a maintainer explicitly asks for one.
- Skip the PR when there is no repeated signal.
- Do not weaken precision-over-recall for dedupe.

## Final Checks

- Re-read the updated `dedupe-issue-local` companion skill and confirm any new clusters are explicit.
- Keep the companion concise; prefer canonical links and short distinguishing notes over long prose.
- Commit any changes on a local branch named `oz-agent/update-dedupe`. Do NOT push the branch; the Python entrypoint will run a write-surface guard and push only when the guard passes.
- If the updates warrant a PR, it will be opened from the pushed branch. Tag `@captainsafia` as a reviewer on that PR.
- Validate any temporary JSON with `jq` before relying on it.
