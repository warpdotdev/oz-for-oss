---
name: verify-pr
description: Run end-to-end verification on a checked-out pull request by discovering and executing the repository's verification skills (skills whose directory name contains `verify` or `test` and whose description covers end-to-end verification), then posting a consolidated report comment. Use when the `/oz-verify` slash command is invoked on a PR.
---

# verify-pr

Run end-to-end verification against a pull request on behalf of the `/oz-verify` slash command.

## Overview

This skill is the entry point for the `/oz-verify` workflow. It does not define what "verification" means for a specific feature — that is each repository's responsibility. Instead it:

1. Discovers verification skills in the repository.
2. Runs each discovered skill against the checked-out PR HEAD.
3. Writes a consolidated markdown report to `verification_report.md`.
4. Posts that report back to the PR as a new comment.

## Convention for verification skills

A skill is treated as a verification skill when **all** of the following are true:

- It lives at `.agents/skills/<name>/SKILL.md` and its directory name contains `verify` or `test` (case-insensitive). Matches include `verify-login/`, `test-checkout/`, `e2e-tests/`, and `smoke-verify/`.
- It exposes standard skill frontmatter (`name`, `description`).
- Its `description` communicates that it performs end-to-end verification / validation / testing of a user-facing feature or flow. Skills whose description is clearly about something unrelated (unit-level helpers, spec authoring, release tooling, etc.) are skipped even if the directory name happens to contain `verify` or `test`.

A naming convention is used — rather than a tag in the frontmatter — so that adding or removing a verification skill is obvious from the repository's directory tree and grep-able during code review. Downstream repositories are free to define their own verification skills; this skill does not prescribe their shape beyond the naming convention and the end-to-end intent signalled by the description.

## Inputs

The prompt provides:

- The pull request number that triggered the run.
- An optional `skill_filter` naming a single verification skill to run. When set, only that skill should be executed and the report should clearly state that discovery was scoped.

The PR is already checked out at HEAD by the calling workflow, so `git`, `gh`, and the repository filesystem are all available.

## Workflow

### 1. Discover verification skills

List every directory under `.agents/skills/` whose name contains `verify` or `test` (case-insensitive) — e.g. `ls .agents/skills/ | grep -iE 'verify|test'` or an equivalent `find` invocation. For each match, read `SKILL.md` and capture:

- `name` from the frontmatter
- `description` from the frontmatter
- the absolute skill path

Then filter that candidate set down to skills whose `description` clearly indicates end-to-end verification, validation, or testing of a user-facing feature or flow. Drop candidates whose description is obviously about something else (spec authoring, unit-level helpers, release tooling, etc.), even if their directory name matched.

If a `skill_filter` is provided, keep only the skill whose `name` matches exactly. If no skill matches, record that fact and continue — the report should make it clear the filter was a no-op.

If no verification skills are found at all, write a short report explaining that no verification skills (skills whose directory name contains `verify` or `test` and whose description covers end-to-end verification) exist in the repository and link to this skill's documentation.

### 2. Run each verification skill

For each discovered skill:

- Follow the skill's own instructions to run it against the PR HEAD.
- Capture pass/fail status.
- Capture any output, logs, or artifact references that would help a reviewer understand the result.
- If a skill fails or errors, continue running the remaining skills — one skill's failure should not block the rest.

Verification is expected to be **read-only**. Do not stage files, create commits, push branches, or modify tracked files. If a verification skill mutates the working tree, leave those changes uncommitted and flag them in the report.

### 3. Write the consolidated report

Write `verification_report.md` at the repository root. Use this structure:

```
## /oz-verify report

Discovered N verification skill(s): `verify-foo`, `verify-bar`.
<optional note when a skill_filter narrowed the set>

### `verify-foo` — ✅ Passed
<one-paragraph summary plus any relevant output>

### `verify-bar` — ❌ Failed
<one-paragraph summary of what failed, with error excerpts>

---
Oz run: <session link if available>
Workflow run: <run URL provided by the caller>
```

Keep each per-skill section short enough to render cleanly as a GitHub comment. Link out to logs or artifacts instead of inlining long output.

### 4. Post the report back to the PR

Post the contents of `verification_report.md` as a new issue comment on the originating PR using `gh api` or `gh pr comment`. Always post a new comment — do not attempt to edit prior `/oz-verify` comments. Treating each run as a fresh comment keeps the audit trail clear and avoids races between concurrent runs.

If posting the comment fails, leave `verification_report.md` on disk and surface the failure in the workflow logs so the fallback step in the workflow can leave a pointer to the run.

## Best Practices

- Prefer running skills in a stable order (alphabetical by skill name) so repeated `/oz-verify` runs produce comparable reports.
- Treat skill-level failures as data to report, not as reasons to abort the whole run.
- Keep the report concise. Long tool output belongs in linked logs, not in the PR comment.
- Never commit or push changes from a `/oz-verify` run.

## Related Skills

- `implement-specs`
- `review-pr`
