---
name: verify-pr
description: Convention document describing how to author a verification skill (`verify-*`) that `/oz-verify` discovers and runs end-to-end against a pull request. Use when authoring a new verification skill or when clarifying what `/oz-verify` expects from each skill it executes.
---

# verify-pr

This skill is not invoked directly by `/oz-verify`. It is the convention document for the ecosystem of **verification skills** that `/oz-verify` discovers and runs on a pull request.

The orchestration loop lives in `.github/scripts/oz_verify.py`. That script:

1. Discovers every skill whose frontmatter sets `verification: "true"` under its top-level `metadata` map.
2. Runs each discovered skill as its own Oz agent run against the PR HEAD.
3. Fetches the `verification_report.md` artifact the skill uploaded.
4. Resolves signed download URLs for any screenshot/video artifacts the skill uploaded, via the Oz SDK.
5. Rewrites standard Markdown image/link URLs in the report that refer to an uploaded artifact's filename with the artifact's signed download URL.
6. Consolidates every skill's report into one comment and posts it back to the PR.

Authors of a verification skill only need to worry about **step 2** — the single Oz run that exercises their verification — and about producing the artifacts that steps 3–5 consume.

## Convention for verification skills

A skill is treated as a verification skill when **all** of the following are true:

- It lives at `.agents/skills/<name>/SKILL.md` and exposes standard skill frontmatter (`name`, `description`).
- Its frontmatter includes a top-level [`metadata`](https://agentskills.io/specification#metadata-field) mapping whose `verification` key is set to `"true"`. This opt-in tag is the single source of truth — neither the skill's directory name nor its description opts it in or out.

Example verification-skill frontmatter:

```
---
name: verify-login
description: Exercise the login flow end-to-end against a checked-out PR and report pass/fail.
metadata:
  verification: "true"
---
```

A dedicated frontmatter tag is used — rather than a naming convention — so that adding or removing a verification skill is an explicit, reviewable decision in the skill's own metadata rather than something implied by a directory name or by how the description happens to be worded. The tag lives under `metadata` to follow the [Agent Skills specification](https://agentskills.io/specification#metadata-field), which reserves `metadata` for client-specific frontmatter extensions like this one. Downstream repositories are free to define their own verification skills; this document does not prescribe their shape beyond the `metadata.verification: "true"` tag and the expectations below.

## Inputs

When `/oz-verify` invokes a verification skill, the Oz agent runs with:

- The PR already checked out at HEAD on the PR's head branch (provided by the workflow).
- Standard `git`, `gh`, and filesystem access inside the checkout.
- A prompt that includes the PR number, requester, the skill's own description, and the contract described below.

The skill should not assume any additional inputs or ambient configuration beyond those — `/oz-verify` is designed so downstream repos can adopt it with only a `WARP_API_KEY` and the standard Oz GitHub App secrets.

## Contract for a verification skill

Each verification skill MUST:

1. **Verify exactly one end-to-end behavior.** Keep each skill narrowly scoped so `/oz-verify` can report per-skill pass/fail rather than a single aggregate result.

2. **Be read-only for tracked files.** Do not stage files, create commits, or push any branch. The PR branch is a read-only input. Untracked evidence (screenshots, videos, logs) may be written to the working tree during the run — conventionally under `verification_artifacts/<skill-name>/` — but must not be committed.

3. **Write `verification_report.md` at the repository root** summarizing the run. The report should:
   - Start with a one-line status: `✅ Passed`, `❌ Failed`, or `⚠️ Errored`.
   - Summarize what was verified and any output worth linking.
   - Reference screenshot/video evidence using a standard Markdown image or link whose URL is the uploaded artifact's filename, e.g. `![login success](login-success.png)` or `[demo](demo.mp4)`. The filename must match the last path component passed to `oz-dev artifact upload`. The workflow rewrites each such URL with a signed download URL drawn from the Oz run's artifacts, so skills must **not** construct image or video URLs themselves.

4. **Upload screenshot/video evidence as Oz artifacts** via:

   ```
   oz-dev artifact upload <path>
   ```

   The subcommand is `artifact` (singular); do not use `artifacts`. Supported media types are PNG, JPEG, GIF, WebP, MP4, MOV, and WebM. Any uploaded artifact that is referenced from the report via a Markdown image or link pointing at its filename stays in place in the consolidated report with its URL rewritten to the signed download URL; any uploaded artifact without such a reference is appended to an **Additional artifacts** section for that skill so the evidence still reaches reviewers.

5. **Upload the report itself as an artifact** so the workflow can fetch it deterministically:

   ```
   oz-dev artifact upload verification_report.md
   ```

6. **Not post the report to the PR.** The workflow collects every skill's report and posts a single consolidated comment so concurrent runs don't race.

## Example `verification_report.md`

```markdown
✅ Passed — login flow completes without errors.

Exercised the email + password login form with the test account. The session
cookie was set and `/home` rendered.

![login success](login-success.png)

<details>
<summary>Console output</summary>

- Test account: `ci-verify@example.com`
- Login roundtrip: 420ms
- No JS errors observed

</details>
```

After the run completes, the workflow rewrites the `login-success.png` URL to the signed download URL for the uploaded artifact of the same filename, then posts a consolidated comment that includes this skill's section alongside the sections from every other discovered verification skill.

## Best practices

- Keep each per-skill report short. Link out to long logs instead of inlining them; keep visual evidence inline via a Markdown image or link so reviewers see it at a glance.
- Prefer filenames that are unique within a single run (e.g. prefix with the skill name) so the workflow's filename-based URL resolution is unambiguous.
- Treat skill-level failures as data to report, not as reasons to abort. The workflow runs each verification skill independently, and the consolidated report surfaces per-skill status so one failure doesn't hide the rest.

## Related skills

- `implement-specs`
- `review-pr`
