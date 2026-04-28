---
name: review-pr
description: Review a pull request diff and write structured feedback to review.json for the workflow to publish. Use when reviewing a checked-out PR from local artifacts like pr_diff.txt and pr_description.txt and producing machine-readable review output instead of posting directly to GitHub.
---

# Review PR Skill

Review the current pull request and write the output to `review.json`.

## Context

- The working directory is the PR branch checkout.
- The workflow usually provides an annotated diff in `pr_diff.txt`.
- The workflow usually provides the PR description in `pr_description.txt`.
- If `spec_context.md` exists, it contains spec context for implementation-vs-spec validation.
- When the prompt references `.agents/skills/review-pr/scripts/resolve_spec_context.py`, use that script to materialize `spec_context.md` on demand instead of expecting spec content to be embedded in the prompt.
- Focus on files and lines changed by this PR.
- Default behavior: do not post comments or reviews to GitHub directly.

## Review Scope

- Prioritize correctness, security, error handling, and meaningful performance issues.
- Always apply the repository's local `security-review-pr` skill as a supplemental security pass on code PRs. Fold any security findings into the same `review.json` produced by this review rather than emitting a separate output.
- When `spec_context.md` exists, use the repository's local `check-impl-against-spec` skill and treat material spec drift as a review concern.
- Include style or nit comments only when you can provide a concrete suggestion block.
- If a concern involves untouched code, mention it in the summary instead of an inline comment.

## Repository-specific overrides

The consuming repository may ship a companion skill at `.agents/skills/review-pr-local/SKILL.md`. When the prompt includes a fenced "Repository-specific guidance" section referencing that companion, read the referenced file and apply its guidance **only** to the categories listed below. Guidance in the companion may never change the output JSON schema, the severity labels, the safety rules, the evidence rules, the suggestion-block constraints, or the diff-line-annotation contract described elsewhere in this skill.

Overridable categories:

- user-facing-string norms
- graceful-degradation preferences for rendering optional dynamic data and error messages
- debugging and observability preferences for error paths
- repo-specific style nits and recurring "what we always flag" patterns
- allowlists of paths to skip

If a companion file is not referenced in the prompt, rely on the core contract alone.

## Diff Line Annotations

The diff file uses these prefixes:

- `[OLD:n]` for deleted lines on the old side. Use `"LEFT"`.
- `[NEW:n]` for added lines on the new side. Use `"RIGHT"`.
- `[OLD:n,NEW:m]` for unchanged context. Use `"RIGHT"` with line `m`.

## Comment Requirements

Every comment body must start with one of these labels:

- `🚨 [CRITICAL]` for bugs, security issues, crashes, or data loss.
- `⚠️ [IMPORTANT]` for logic problems, edge cases, or missing error handling.
- `💡 [SUGGESTION]` for worthwhile improvements or better patterns.
- `🧹 [NIT]` for cleanup only when the comment includes a suggestion block.

Write comments with these constraints:

- Be concise, direct, and actionable.
- Do not add compliments or hedging.
- Prefer single-line comments.
- Keep ranges to at most 10 lines.
- Restrict inline comments to valid changed lines in this PR.
- Only create file-level or inline comments for files that exist in this PR's diff.
- If the relevant file or line is not part of the diff, put the feedback in `summary` instead of `comments`.

## Suggestion Blocks

When proposing a code change, use:

```suggestion
<replacement code here>
```

Rules:

- Match the exact indentation of the original file.
- Include only replacement code.
- The block content replaces **exactly** the lines `start_line`–`line` inclusive. Every line inside the block becomes the new file content for that range, and GitHub leaves all other lines untouched.
- Do **not** include lines outside that range. Lines above `start_line` and below `line` remain in the file; repeating them inside the block causes them to appear twice after the suggestion is committed.
- Never open the block with a line that already appears immediately above `start_line`, and never close the block with a line that already appears immediately below `line`. If you need those lines as anchors, widen `start_line` or `line` so they are actually part of the replaced range.
- Count brace, bracket, paren, and block-delimiter depth (`{`, `[`, `(`, `end`, etc.) across the original replaced lines and ensure the replacement ends at the same depth. Do not emit phantom closing tokens, and do not drop required ones.
- When unsure of the surrounding context, widen `start_line`/`line` to include enough real lines from the diff rather than guessing at surrounding tokens.
- For multi-line suggestions, set `start_line` to the first line and `line` to the last line.

## Output Format

Create `review.json` with this shape:

```json
{
  "summary": "## Overview\n...\n\n## Concerns\n- ...\n\n## Verdict\nFound: 1 critical, 2 important, 3 suggestions\n\n**Request changes**",
  "comments": [
    {
      "path": "path/to/file",
      "line": 42,
      "side": "RIGHT",
      "start_line": 40,
      "body": "⚠️ [IMPORTANT] Short explanation\n\n```suggestion\nreplacement\n```"
    }
  ]
}
```

Field rules:

- `path` must be relative to the repository root.
- `line` is required and must target the correct side.
- `start_line` is optional and only for multi-line ranges.
- `side` must be `"LEFT"` or `"RIGHT"`.

## Summary Requirements

The `summary` must include:

- A high-level overview of the PR.
- Important concerns and any untouched-code concerns that could not be commented inline.
- Issue counts in the format `Found: X critical, Y important, Z suggestions`.
- A final recommendation of `Approve`, `Approve with nits`, or `Request changes`.

## Final Checks

Before finishing:

- Validate `review.json` with `jq`.
- Fix invalid JSON if validation fails.
- Confirm line numbers match the annotated diff.
- Do not run `gh pr review`, `gh pr comment`, `gh api`, or any other command that posts to GitHub.

Your only output is the final `review.json`.

## Cloud workflow mode

If the prompt says you are in a cloud-environment workflow and the expected local context files are missing:

- Create `pr_description.txt` yourself from the PR body or GitHub metadata provided in the prompt.
- Fetch and check out the exact PR head branch by name before generating the diff. Run:
    ```
    git fetch origin <head_branch>
    git checkout <head_branch>
    ```
  Do NOT use `FETCH_HEAD` — always reference the named branch.
- Generate the diff against the base branch using a three-dot merge-base diff:
    ```
    git diff origin/<base_branch>...HEAD
    ```
  This isolates only the changes introduced by the PR, not accumulated state from other branches.
- Convert the raw diff into `pr_diff.txt` using the annotated format above before reviewing.
- If the prompt provides a `resolve_spec_context.py` command, run it only when spec validation is needed and write any returned spec content to `spec_context.md` before running review.
- Still produce `review.json` and validate it with `jq`.
- When the host already populated `pr_description.txt`, `pr_diff.txt`, or `spec_context.md` in the workflow checkout, use those files as-is and do not try to re-fetch GitHub context yourself.
- The cloud run does not receive `GH_TOKEN`. If the host did not pre-materialize the needed context, follow only the prompt's explicit fallback instructions.
- After validation, upload the result via `oz artifact upload review.json` (or `oz-preview artifact upload review.json` if the `oz` CLI is not available). Either CLI is acceptable — use whichever one is installed in the environment. Do not write `review.json` to a `/mnt/...` mount path — the cloud agent has no such mount, and the host workflow only reads what you upload through the artifact CLI.
- IMPORTANT: the upload subcommand is `artifact` (singular) on both `oz` and `oz-preview`. Do not use `artifacts` (plural) — that is not a valid subcommand and will fail.
