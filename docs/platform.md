# Platform workflows in `oz-for-oss`

The repository is organized around a small set of skill-backed agent roles, all delivered through the Vercel webhook control plane. GitHub remains the durable state store — issues, labels, assignees, comments, pull requests, branches, and reviews — but the runtime that decides when to run and how to apply results is `api/webhook.py` plus `api/cron.py`.

The behavior lives in three layers:

- Oz-specific skills in [`../.agents/skills/`](../.agents/skills/) plus shared base skills resolved by name
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

The spec-writing role uses shared `spec-driven-implementation`, `write-product-spec`, and `write-tech-spec` skills, plus the Oz wrapper skills [`create-product-spec`](../.agents/skills/create-product-spec/SKILL.md) and [`create-tech-spec`](../.agents/skills/create-tech-spec/SKILL.md). The durable outputs are product and tech specs under [`../specs/`](../specs/).

### Implementation

The implementation role uses the Oz wrapper [`implement-issue`](../.agents/skills/implement-issue/SKILL.md) plus shared `implement-specs` and `spec-driven-implementation` skills. It prefers approved spec context when available and refuses unapproved spec PRs when the workflow detects them.

### Review and verification

The review role uses the shared `review-pr` and `check-impl-against-spec` skills plus the Oz-specific [`review-spec`](../.agents/skills/review-spec/SKILL.md). PR review results are uploaded as `review.json` and applied by `core/workflows/review_pr.py`.

The verification role uses [`verify-pr`](../.agents/skills/verify-pr/SKILL.md) and runs from the `/oz-verify` slash command on PR comments.

### PR comment response

The `respond-to-pr-comment` workflow handles `@oz-agent` mentions (and the legacy `@warp-agent` alias, kept after the rebrand) on PR conversations, inline review comments, and review bodies. It uses the implementation skill family with PR context and any available spec context.

When a PR comment, review comment, or review body mentions an agent-*like* handle that is not recognized — for example the pre-rebrand `@warp-bot` typo or a mistyped `@ozagent` — the `acknowledge-unknown-mention` synchronous path posts a one-shot comment letting the commenter know the mention was received but not recognized and points them at `@oz-agent`, instead of silently doing nothing.

## Repo-local companions

Each reusable role can have a repo-local companion skill, such as [`review-pr-local`](../.agents/skills/review-pr-local/SKILL.md), [`review-spec-local`](../.agents/skills/review-spec-local/SKILL.md), [`triage-issue-local`](../.agents/skills/triage-issue-local/SKILL.md), and [`dedupe-issue-local`](../.agents/skills/dedupe-issue-local/SKILL.md). The prompt helpers detect these files in the consuming repository and reference them when present, while absent or frontmatter-only companions are treated as no-op.

## Self-improvement loops

The `update-*` skills can be used by a [scheduled Oz agent](https://docs.warp.dev/agent-platform/cloud-agents/triggers/scheduled-agents-quickstart/) to improve your repo-local skills based on recent human feedback in GitHub.

[`update-triage`](../.agents/skills/update-triage/SKILL.md) learns from maintainer re-labels, re-opens, and follow-up comments on triaged issues. [`update-pr-review`](../.agents/skills/update-pr-review/SKILL.md) learns from replies to agent-authored PR review comments and broader reviewer feedback. [`update-dedupe`](../.agents/skills/update-dedupe/SKILL.md) learns from repeated closed-as-duplicate signals that point to the same canonical issue.

Each loop has a narrow write surface and keeps the core cross-repo skills stable. When a repeated signal is strong enough, the scheduled run updates only the relevant repo-local companion, and in the triage case may also update the issue label taxonomy.

## Non-agent webhook paths

Some routed webhook branches perform deterministic GitHub mutations without dispatching an Oz run:

- `announce-ready-issue` posts fixed availability guidance when `ready-to-spec` or `ready-to-implement` is added without assigning `oz-agent`.
- `acknowledge-unknown-mention` posts a one-shot comment when a PR mention uses an unrecognized but agent-like handle (e.g. `@warp-bot`), so the mention is acknowledged rather than dropped.
- `plan-approved` performs approval bookkeeping synchronously and only falls through to implementation dispatch when the linked issue is ready.

## In one sentence

`oz-for-oss` is a webhook-delivered OSS automation platform that feeds rich GitHub and repository context into skill-backed Oz agent roles for triage, planning, implementation, review, verification, and PR follow-up.
