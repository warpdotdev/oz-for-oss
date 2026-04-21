---
name: verify-pr
description: Run end-to-end verification on a checked-out pull request by discovering and executing the repository's verification skills (skills whose frontmatter declares `verification: true`), collecting any screenshot/video artifacts they produce, and posting a consolidated report comment that embeds those artifacts. Use when the `/oz-verify` slash command is invoked on a PR.
---

# verify-pr

Run end-to-end verification against a pull request on behalf of the `/oz-verify` slash command.

## Overview

This skill is the entry point for the `/oz-verify` workflow. It does not define what "verification" means for a specific feature — that is each repository's responsibility. Instead it:

1. Discovers verification skills in the repository.
2. Runs each discovered skill against the checked-out PR HEAD.
3. Collects any screenshot or video artifacts the skills produce.
4. Writes a consolidated markdown report to `verification_report.md`.
5. Posts that report — with artifacts embedded inline — back to the PR as a new comment.

## Convention for verification skills

A skill is treated as a verification skill when **all** of the following are true:

- It lives at `.agents/skills/<name>/SKILL.md` and exposes standard skill frontmatter (`name`, `description`).
- Its frontmatter declares `verification: true` (top-level boolean field). This is the single source of truth — neither the skill's directory name nor its description opts it in or out.

A frontmatter tag is used — rather than a naming convention — so that adding or removing a verification skill is an explicit, reviewable decision in the skill's own metadata rather than something implied by a directory name or by how the description happens to be worded. Downstream repositories are free to define their own verification skills; this skill does not prescribe their shape beyond the `verification: true` tag and the expectation that they perform end-to-end verification of the checked-out PR.

Example verification-skill frontmatter:

```
---
name: verify-login
description: Exercise the login flow end-to-end against a checked-out PR and report pass/fail.
verification: true
---
```

## Inputs

The prompt provides:

- The pull request number that triggered the run.
- An optional `skill_filter` naming a single verification skill to run. When set, only that skill should be executed and the report should clearly state that discovery was scoped.

The PR is already checked out at HEAD by the calling workflow, so `git`, `gh`, and the repository filesystem are all available.

## Workflow

### 1. Discover verification skills

List every `.agents/skills/*/SKILL.md` in the repository. For each one, parse the YAML frontmatter and keep it only when the frontmatter has `verification: true`.

A grep-friendly first pass (`grep -lE '^verification:\s*true' .agents/skills/*/SKILL.md`) is fine; confirm with a proper YAML parse before treating a match as authoritative, since a matching line outside the frontmatter block is not a valid opt-in.

For each matching skill, capture:

- `name` from the frontmatter
- `description` from the frontmatter
- the absolute skill path

If a `skill_filter` is provided, keep only the skill whose `name` matches exactly. If no skill matches, record that fact and continue — the report should make it clear the filter was a no-op.

If no verification skills are found at all, write a short report explaining that no skills declare `verification: true` in their frontmatter and link to this skill's documentation.

### 2. Run each verification skill

For each discovered skill:

- Follow the skill's own instructions to run it against the PR HEAD.
- Capture pass/fail status.
- Capture any output, logs, or artifact references that would help a reviewer understand the result.
- If a skill fails or errors, continue running the remaining skills — one skill's failure should not block the rest.

Verification is expected to be **read-only** for tracked files. Do not stage files, create commits on the PR branch, push the PR branch, or modify tracked files. Verification skills may write screenshot/video artifacts to the working tree (see next step); those files are not intended to be committed to the PR branch.

### 3. Collect screenshot and video artifacts

Verification skills may produce screenshots or short videos as evidence (e.g. a screenshot of a rendered UI, a short screen recording of a flow). By convention, verification skills write these to `verification_artifacts/<skill-name>/` relative to the repository root, but the skill also scans the whole working tree for untracked media files so skills that use other locations still work.

After all verification skills have run, scan the working tree for untracked files with any of these extensions (case-insensitive):

- Images: `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`
- Videos: `.mp4`, `.mov`, `.webm`

Use `git ls-files --others --exclude-standard` to restrict the scan to untracked files so tracked media already in the repository is never republished. Group each matched file by the verification skill that produced it (by path prefix when possible, otherwise under a shared `misc/` bucket).

For every matched file, publish it to a dedicated long-lived branch so the PR comment can reference it via a stable public raw URL:

- Branch name: `oz-verify-artifacts` (a single orphan branch that accumulates runs; create it if it does not exist yet).
- Path within the branch: `pr-<pr_number>/run-<run_id>/<skill-name>/<original-filename>`. Use `${GITHUB_RUN_ID}` from the environment for `<run_id>` when available; otherwise fall back to a timestamp.
- Commit message: `verify-pr: artifacts for PR #<pr_number> run <run_id>`.

Use `git worktree add` to stage the push without disturbing the PR checkout, push with the workflow's `GITHUB_TOKEN`, and construct raw URLs of the form:

```
https://raw.githubusercontent.com/<owner>/<repo>/oz-verify-artifacts/pr-<pr_number>/run-<run_id>/<skill-name>/<filename>
```

If pushing to `oz-verify-artifacts` fails (for example, because the workflow does not have `contents: write`), skip the embed step and fall back to listing the artifacts as plain filenames in the report along with a note about the missing permission. Do not block the rest of the run on artifact publishing.

### 4. Write the consolidated report

Write `verification_report.md` at the repository root. Use this structure:

```
## /oz-verify report

Discovered N verification skill(s): `verify-foo`, `verify-bar`.
<optional note when a skill_filter narrowed the set>

### `verify-foo` — ✅ Passed
<one-paragraph summary plus any relevant output>

**Artifacts:**

![login-success](https://raw.githubusercontent.com/<owner>/<repo>/oz-verify-artifacts/pr-123/run-456/verify-foo/login-success.png)

<video src="https://raw.githubusercontent.com/<owner>/<repo>/oz-verify-artifacts/pr-123/run-456/verify-foo/login.mp4" controls></video>

### `verify-bar` — ❌ Failed
<one-paragraph summary of what failed, with error excerpts>

---
Oz run: <session link if available>
Workflow run: <run URL provided by the caller>
```

Embed images with standard Markdown image syntax (`![alt](url)`) so GitHub renders them inline. Embed videos with an HTML `<video controls>` tag pointing at the raw URL; GitHub's comment renderer honors the tag for common container formats. When artifacts could not be published (for example, the branch push failed), list them as plain filenames instead and note why they are not embedded.

Keep each per-skill section short enough to render cleanly as a GitHub comment. Link out to logs instead of inlining long text output; keep visual evidence inline as embeds because that is what a reviewer most wants to see at a glance.

### 5. Post the report back to the PR

Post the contents of `verification_report.md` as a new issue comment on the originating PR using `gh api` or `gh pr comment`. Always post a new comment — do not attempt to edit prior `/oz-verify` comments. Treating each run as a fresh comment keeps the audit trail clear and avoids races between concurrent runs.

If posting the comment fails, leave `verification_report.md` on disk and surface the failure in the workflow logs so the fallback step in the workflow can leave a pointer to the run.

## Best Practices

- Prefer running skills in a stable order (alphabetical by skill name) so repeated `/oz-verify` runs produce comparable reports.
- Treat skill-level failures as data to report, not as reasons to abort the whole run.
- Keep the report concise. Long tool output belongs in linked logs, not in the PR comment; visual artifacts belong inline.
- Never commit or push to the PR branch from a `/oz-verify` run. Pushing artifacts to the dedicated `oz-verify-artifacts` branch is the only sanctioned write.

## Related Skills

- `implement-specs`
- `review-pr`
