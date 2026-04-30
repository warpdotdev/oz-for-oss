# Platform workflows in `oz-for-oss`

The repository is organized around a small set of skill-backed agent roles, all delivered through the Vercel webhook control plane. GitHub remains the durable state store — issues, labels, assignees, comments, pull requests, branches, and reviews — but the runtime that decides when to run and how to apply results is `api/webhook.py` plus `api/cron.py`.

The behavior lives in three layers:

- skills in [`../.agents/skills/`](../.agents/skills/)
- workflow-specific context, prompt, and apply helpers in [`../core/workflows/`](../core/workflows/)
- shared Oz and GitHub helpers in [`../core/oz/`](../core/oz/)

The `.github` directory is now configuration and CI only: [`STAKEHOLDERS`](../.github/STAKEHOLDERS), [`issue-triage/config.json`](../.github/issue-triage/config.json), [`oz/config.yml`](../.github/oz/config.yml), and [`workflows/run-tests.yml`](../.github/workflows/run-tests.yml).

## How a workflow uses an agent

Agent-backed workflows follow the same lifecycle:

1. `core/routing.py` maps a GitHub webhook delivery to a workflow.
2. A concrete workflow class in `core/workflows/` gathers GitHub context and builds a prompt using helpers from `core/workflows/`.
3. `core/dispatch.py` starts an Oz cloud run and persists `RunState` in Vercel KV.
4. A post-dispatch hook creates or updates the progress comment with the Oz run id as the canonical metadata identity.
5. `api/cron.py` polls the Oz run, records session links while it runs, loads artifacts on success, and invokes the workflow-specific result applier.

This keeps prompt construction and result application workflow-specific while sharing lifecycle plumbing across review, triage, spec, implementation, verification, and PR-comment response flows.

## Core roles

### Triage

The triage role uses [`triage-issue`](../.agents/skills/triage-issue/SKILL.md), optional duplicate detection via [`dedupe-issue`](../.agents/skills/dedupe-issue/SKILL.md), the label taxonomy in [`config.json`](../.github/issue-triage/config.json), and ownership hints from [`STAKEHOLDERS`](../.github/STAKEHOLDERS). It handles new issues, `@oz-agent` mentions on plain issues, and `needs-info` replies from the original reporter.

### Spec writing

The spec-writing role uses [`spec-driven-implementation`](../.agents/skills/spec-driven-implementation/SKILL.md), [`create-product-spec`](../.agents/skills/create-product-spec/SKILL.md), [`create-tech-spec`](../.agents/skills/create-tech-spec/SKILL.md), [`write-product-spec`](../.agents/skills/write-product-spec/SKILL.md), and [`write-tech-spec`](../.agents/skills/write-tech-spec/SKILL.md). The durable outputs are product and tech specs under [`../specs/`](../specs/).

### Implementation

The implementation role uses [`implement-issue`](../.agents/skills/implement-issue/SKILL.md), [`implement-specs`](../.agents/skills/implement-specs/SKILL.md), and [`spec-driven-implementation`](../.agents/skills/spec-driven-implementation/SKILL.md). It prefers approved spec context when available and refuses unapproved spec PRs when the workflow detects them.

### Review and verification

The review role uses [`review-pr`](../.agents/skills/review-pr/SKILL.md), [`review-spec`](../.agents/skills/review-spec/SKILL.md), and spec consistency checks via [`check-impl-against-spec`](../.agents/skills/check-impl-against-spec/SKILL.md). PR review results are uploaded as `review.json` and applied by `core/workflows/review_pr.py`.

The verification role uses [`verify-pr`](../.agents/skills/verify-pr/SKILL.md) and runs from the `/oz-verify` slash command on PR comments.

### PR comment response

The `respond-to-pr-comment` workflow handles `@oz-agent` mentions on PR conversations, inline review comments, and review bodies. It uses the implementation skill family with PR context and any available spec context.

## Repo-local companions

Each reusable role can have a repo-local companion skill, such as [`review-pr-local`](../.agents/skills/review-pr-local/SKILL.md), [`review-spec-local`](../.agents/skills/review-spec-local/SKILL.md), [`triage-issue-local`](../.agents/skills/triage-issue-local/SKILL.md), and [`dedupe-issue-local`](../.agents/skills/dedupe-issue-local/SKILL.md). The prompt helpers detect these files in the consuming repository and reference them when present, while absent or frontmatter-only companions are treated as no-op.

## Non-agent webhook paths

Some routed webhook branches perform deterministic GitHub mutations without dispatching an Oz run:

- `announce-ready-issue` posts fixed availability guidance when `ready-to-spec` or `ready-to-implement` is added without assigning `oz-agent`.
- `plan-approved` performs approval bookkeeping synchronously and only falls through to implementation dispatch when the linked issue is ready.

## In one sentence

`oz-for-oss` is a webhook-delivered OSS automation platform that feeds rich GitHub and repository context into skill-backed Oz agent roles for triage, planning, implementation, review, verification, and PR follow-up.
