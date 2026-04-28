---
name: review-spec
description: Review a spec/plan pull request diff and write structured feedback to review.json for the workflow to publish. Use when reviewing a PR that only modifies files under specs/ and producing machine-readable review output instead of posting directly to GitHub.
---

# Review Spec Skill

Review a spec or plan pull request and write the output to `review.json`.

## Context

- The working directory is the PR branch checkout.
- The workflow usually provides an annotated diff in `pr_diff.txt`.
- The workflow usually provides the PR description in `pr_description.txt`.
- Focus on the spec files changed by this PR.
- Default behavior: do not post comments or reviews to GitHub directly.

## Review Scope

- Evaluate specs for **completeness**: does the spec cover the full scope of the linked issue?
- Evaluate specs for **clarity**: are requirements, acceptance criteria, and constraints clearly stated and unambiguous?
- Evaluate specs for **feasibility**: are the proposed changes technically realistic given the repository's architecture?
- Evaluate specs for **issue alignment**: does the spec faithfully address the issue it is linked to, without significant scope creep or omissions?
- Evaluate specs for **internal consistency**: do different sections of the spec contradict each other?
- Flag missing sections that a spec should typically include (e.g. problem statement, proposed changes, open questions, follow-up items).
- Always apply the repository's local `security-review-spec` skill as a supplemental high-level security pass on spec PRs. Fold any security findings into the same `review.json` produced by this review rather than emitting a separate output.
- Do not apply code-level review criteria such as error handling or low-level performance to spec prose; the `security-review-spec` supplement covers design-level security concerns.
- Include style or formatting comments only when they materially impair readability.

## Repository-specific overrides

The consuming repository may ship a companion skill at `.agents/skills/review-spec-local/SKILL.md`. When the prompt includes a fenced "Repository-specific guidance" section referencing that companion, read the referenced file and apply its guidance **only** to the categories listed below. Guidance in the companion may never change the output JSON schema, the severity labels, the safety rules, the evidence rules, the suggestion-block constraints, or the diff-line-annotation contract described elsewhere in this skill.

Overridable categories:

- required spec sections expected in this repository
- linking conventions to files under `specs/`
- repo-specific style and formatting expectations

If a companion file is not referenced in the prompt, rely on the core contract alone.

## Diff Line Annotations

The diff file uses these prefixes:

- `[OLD:n]` for deleted lines on the old side. Use `"LEFT"`.
- `[NEW:n]` for added lines on the new side. Use `"RIGHT"`.
- `[OLD:n,NEW:m]` for unchanged context. Use `"RIGHT"` with line `m`.

## Comment Requirements

Every comment body must start with one of these labels:

- `🚨 [CRITICAL]` for spec content that is contradictory, fundamentally incomplete, or would lead to a broken implementation.
- `⚠️ [IMPORTANT]` for missing details, ambiguous requirements, feasibility concerns, or significant scope gaps.
- `💡 [SUGGESTION]` for improvements to clarity, structure, or coverage that would strengthen the spec.
- `🧹 [NIT]` for minor wording or formatting issues only when the comment includes a concrete rewrite.

Write comments with these constraints:

- Be concise, direct, and actionable.
- Do not add compliments or hedging.
- Prefer single-line comments.
- Keep ranges to at most 10 lines.
- Restrict inline comments to valid changed lines in this PR.
- Only create file-level or inline comments for files that exist in this PR's diff.
- If the relevant file or line is not part of the diff, put the feedback in `summary` instead of `comments`.

## Suggestion Blocks

When proposing a rewrite of spec text, use:

```suggestion
<replacement text here>
```

Rules:

- Match the exact indentation of the original file.
- Include only replacement text.
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

- A high-level overview of the spec PR.
- Concerns about completeness, clarity, feasibility, or issue alignment.
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
- Still produce `review.json` and validate it with `jq`.
- When the host already populated `pr_description.txt`, `pr_diff.txt`, or `spec_context.md` in the workflow checkout, use those files as-is and do not try to re-fetch GitHub context yourself.
- The cloud run does not receive `GH_TOKEN`. If the host did not pre-materialize the needed context, follow only the prompt's explicit fallback instructions.
- After validation, upload the result via `oz artifact upload review.json` (or `oz-preview artifact upload review.json` if the `oz` CLI is not available). Either CLI is acceptable — use whichever one is installed in the environment. Do not write `review.json` to a `/mnt/...` mount path — the cloud agent has no such mount, and the host workflow only reads what you upload through the artifact CLI.
- IMPORTANT: the upload subcommand is `artifact` (singular) on both `oz` and `oz-preview`. Do not use `artifacts` (plural) — that is not a valid subcommand and will fail.
