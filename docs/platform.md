# Platform workflows in `oz-for-oss`

The most useful way to understand this repository is not as a pile of workflows. It is as a small set of agent roles, each backed by repository-specific skills and prompts, with two delivery surfaces that get the right context in front of the right agent: a webhook control plane for PR-triggered work and the GitHub Actions wrappers for issue-triggered, plan-approval, and self-improvement work.

The real behavior lives one layer down from the delivery wiring:

- skills in [`../.agents/skills/`](../.agents/skills/)
- prompt construction in the helpers under [`../lib/scripts/`](../lib/scripts/) and [`../.github/scripts/`](../.github/scripts/)
- shared Oz and GitHub helpers in [`../lib/oz_workflows/`](../lib/oz_workflows/)

That is the center of gravity for the platform. The delivery surfaces mostly decide when to run, what permissions to grant, and what repository state to pass in. The agents decide how to reason about that state.

## The agent model

This platform has a few recurring agent roles.

There is a triage agent that turns an issue into structured repository state. There are spec-writing agents that turn issue intent into durable planning artifacts. There is an implementation agent that turns approved intent into branch changes. There is a review agent that evaluates pull requests. And there is a self-improvement agent that updates the review instructions themselves based on reviewer feedback.

Those roles matter more than any single workflow file because the same role can appear in multiple entrypoints. A workflow can change, a trigger can move, or a local adapter can disappear, but the underlying agent role is what gives the system its shape.

GitHub is still the control plane. Issues, labels, assignees, comments, pull requests, and branches are the durable state. But the intelligence in the system comes from the prompts and skills that teach Oz how to read that state and what to do with it.

## How a workflow uses an agent

Most of the agent-backed workflows in this repo follow the same pattern:

1. a delivery surface decides that a unit of work should run — either the webhook receiver under [`../api/webhook.py`](../api/webhook.py), or a reusable workflow in [`../.github/workflows/`](../.github/workflows/)
2. a Python helper gathers context from GitHub and the repository
3. that helper assembles a task-specific prompt
4. the dispatcher invokes Oz against one or more local skills in [`../.agents/skills/`](../.agents/skills/)
5. the result is applied back to GitHub as labels, comments, branches, PRs, or reviews

For PR-triggered workflows the webhook returns 202 within ~100 ms after stashing the in-flight Oz run id in Vercel KV; a 1-minute cron tick handles the apply step. The agent uploads its result via `oz artifact upload <name>.json` and the cron-side handler downloads it through `oz_workflows.artifacts.load_*_artifact`. Issue-triggered and plan-approval workflows still run end-to-end inside the GitHub Actions runner because their work involves cloning the repo, pushing branches, and opening pull requests.

That prompt-construction layer is important. The repo does not just say “run the review skill” or “run the triage skill.” It builds a concrete packet of issue or PR context around the skill:

- issue body and comments
- label taxonomy from [`config.json`](../.github/issue-triage/config.json)
- ownership hints from [`STAKEHOLDERS`](../.github/STAKEHOLDERS)
- spec context from [`../specs/`](../specs/)
- PR metadata, diff information, and prior review context

So the workflows are best read as orchestration wrappers around skill-backed prompts.

## The triage agent

The first major agent role is the triage agent. It is grounded in [`triage-issue`](../.agents/skills/triage-issue/SKILL.md) and supplemented by [`dedupe-issue`](../.agents/skills/dedupe-issue/SKILL.md). The reusable workflow wrapper is [`triage-new-issues.yml`](../.github/workflows/triage-new-issues.yml), implemented by [`triage_new_issues.py`](../.github/scripts/triage_new_issues.py).

This agent’s job is not just to classify issues. Its real job is to turn a raw issue into something the rest of the system can trust. That means it has to separate observed symptoms from user hypotheses, normalize the issue body, apply managed labels, infer likely owners, check for duplicates, and decide whether more information is needed.

An important part of that role is clarification. In this platform, clarifying questions are part of triage rather than a separate afterthought. When an issue is underspecified, the triage agent is expected to ask the next useful questions and then re-triage once the reporter replies.

There is also a narrower follow-up path in [`respond-to-triaged-issue-comment.yml`](../.github/workflows/respond-to-triaged-issue-comment.yml), implemented by [`respond_to_triaged_issue_comment.py`](../.github/scripts/respond_to_triaged_issue_comment.py). That workflow still relies on the same analytical skill family, but it is operating in a more conversational mode: answer or interpret a follow-up comment on an already triaged issue without rerunning the full mutation path.

The triage agent runs as a GitHub Actions workflow because it routinely needs to fan out across multiple GitHub mutations (label, comment, assignment) inside a single transactional run, which is easier to express as a long-running Actions job than as a fire-and-forget cloud agent dispatch.

## The spec-writing agents

Once an issue is clear enough, the next important roles are the spec-writing agents. They are grounded in:

- [`spec-driven-implementation`](../.agents/skills/spec-driven-implementation/SKILL.md)
- [`create-product-spec`](../.agents/skills/create-product-spec/SKILL.md)
- [`create-tech-spec`](../.agents/skills/create-tech-spec/SKILL.md)
- [`write-product-spec`](../.agents/skills/write-product-spec/SKILL.md)
- [`write-tech-spec`](../.agents/skills/write-tech-spec/SKILL.md)

The reusable workflow wrapper is [`create-spec-from-issue.yml`](../.github/workflows/create-spec-from-issue.yml), implemented by [`create_spec_from_issue.py`](../.github/scripts/create_spec_from_issue.py).

These agents are doing something very specific: they translate issue discussion into durable repository artifacts under [`../specs/`](../specs/). The output is not just generated prose in a chat transcript. The output is committed planning state that other humans and agents can review, approve, and build against.

That is why the spec-writing role matters as its own abstraction. The workflow itself mostly handles assignment, branch creation, and PR creation after the agent has finished pushing the branch and handing back any PR metadata the workflow needs. The actual planning behavior — what belongs in a product spec, what belongs in a tech spec, how much to ground in existing code, how to distinguish user-facing behavior from implementation detail — comes from the skills.

## The implementation agent

After planning comes the implementation agent. It is grounded in:

- [`implement-issue`](../.agents/skills/implement-issue/SKILL.md)
- [`implement-specs`](../.agents/skills/implement-specs/SKILL.md)
- [`spec-driven-implementation`](../.agents/skills/spec-driven-implementation/SKILL.md)

The reusable workflow wrapper is [`create-implementation-from-issue.yml`](../.github/workflows/create-implementation-from-issue.yml), implemented by [`create_implementation_from_issue.py`](../.github/scripts/create_implementation_from_issue.py).

This agent is not meant to freewheel off an issue description alone when stronger design context exists. The surrounding prompt assembly is explicit about using approved spec context when available, and about refusing to continue when spec PRs exist but are not yet approved. That constraint is not just workflow policy. It is part of the implementation agent’s contract with the rest of the system.

So the implementation role is best understood as: take approved intent, the current branch state, and repository validation expectations, then produce branch changes in the right place. The agent's job stops at the branch push plus any requested handoff artifacts; the workflow handles PR creation or refresh separately. The agent handles the reasoning that turns issue and spec context into code.

## The review agent

The review role is grounded in:

- [`review-pr`](../.agents/skills/review-pr/SKILL.md)
- [`review-spec`](../.agents/skills/review-spec/SKILL.md)
- [`check-impl-against-spec`](../.agents/skills/check-impl-against-spec/SKILL.md)

The webhook control plane is the only delivery surface for review now. [`api/webhook.py`](../api/webhook.py) routes `pull_request`, `pull_request_review_comment`, and PR-conversation `issue_comment` events to [`build_review_request`](../lib/builders.py); the cron poller applies the resulting `review.json` back to the PR through [`apply_review_result`](../lib/scripts/review_pr.py). Spec-only PRs and code PRs are still reviewed differently, and the prompt layer is careful about producing structured review output rather than free-form chat.

There is also a webhook-driven branch-follow-up path for [`respond-to-pr-comment`](../lib/builders.py) (the `@oz-agent` mention on a PR review comment, conversation comment, or review body) and a verification path for [`verify-pr-comment`](../lib/builders.py) (the `/oz-verify` slash command). Both reuse the implementation skill family on top of the same dispatch + cron drain plumbing.

## Core skills and repo-local companions

Each reusable agent role has a **core skill** in [`../.agents/skills/<agent>/SKILL.md`](../.agents/skills/) and, when repo-tunable behavior exists, a paired **repo-local companion** in [`../.agents/skills/<agent>-local/SKILL.md`](../.agents/skills/). The core skill expresses the cross-repo contract — output schema, severity labels, safety rules, evidence rules — and is treated as read-only from the self-improvement loops. The companion specializes only the override categories the core skill explicitly enumerates (for example `review-pr`'s user-facing-string norms, or `triage-issue`'s label taxonomy).

The initial companion skills are:

- [`review-pr-local`](../.agents/skills/review-pr-local/SKILL.md)
- [`review-spec-local`](../.agents/skills/review-spec-local/SKILL.md)
- [`triage-issue-local`](../.agents/skills/triage-issue-local/SKILL.md)
- [`dedupe-issue-local`](../.agents/skills/dedupe-issue-local/SKILL.md)

At prompt assembly time, the helpers in [`../lib/scripts/`](../lib/scripts/) and [`../.github/scripts/`](../.github/scripts/) call `resolve_repo_local_skill_path(workspace, core_skill_name)` to detect a companion in the consuming repository's workspace. When the file exists and contains non-frontmatter body content, the helper appends a fenced "Repository-specific guidance" section to the prompt that *references* the companion path. The companion body is never inlined into the prompt; the agent reads the referenced file directly via its usual skill-read path. When no companion is present, the section is omitted entirely and the agent falls back to the core contract alone.

A consuming repository that has not ingested any repo-specific guidance yet can adopt `oz-for-oss` unchanged: the prompt-construction layer treats absent or effectively empty (frontmatter-only) companions as absent and runs the core skills with no special wiring. See [`bootstrap-issue-config`](../.agents/skills/bootstrap-issue-config/SKILL.md) for how new repositories scaffold empty companion skills during onboarding.

## The self-improvement agents

The last core role is self-improvement. Rather than a single loop, the platform ships a small family of loops, each scoped to a narrow repo-local companion. They run as scheduled GitHub Actions workflows, not webhook events, because they are not user-driven and they only fire on a weekly cron:

- [`update-pr-review`](../.agents/skills/update-pr-review/SKILL.md) writes only to [`review-pr-local`](../.agents/skills/review-pr-local/SKILL.md) and [`review-spec-local`](../.agents/skills/review-spec-local/SKILL.md), implemented by [`update_pr_review.py`](../.github/scripts/update_pr_review.py) and driven by [`update-pr-review.yml`](../.github/workflows/update-pr-review.yml).
- [`update-triage`](../.agents/skills/update-triage/SKILL.md) writes only to [`triage-issue-local`](../.agents/skills/triage-issue-local/SKILL.md) and [`.github/issue-triage/*`](../.github/issue-triage/), implemented by [`update_triage.py`](../.github/scripts/update_triage.py) and driven by [`update-triage.yml`](../.github/workflows/update-triage.yml).
- [`update-dedupe`](../.agents/skills/update-dedupe/SKILL.md) writes only to [`dedupe-issue-local`](../.agents/skills/dedupe-issue-local/SKILL.md), implemented by [`update_dedupe.py`](../.github/scripts/update_dedupe.py) and driven by [`update-dedupe.yml`](../.github/workflows/update-dedupe.yml).

These loops do not review application code or specs directly. Instead, each one reads a narrow signal (PR review feedback, maintainer triage overrides, closed-as-duplicate events) and proposes minimum-viable edits to the corresponding companion skill. The Python entrypoint gates the push behind a `git diff` guard: any change outside the declared write surface aborts the run before a PR is opened. The core skill files and the shared workflow scripts are never writable from a self-improvement loop.

## The workflows that are not really agents

Looking at the platform through agent roles also makes it clearer which pieces are not agents at all.

[`comment-on-unready-assigned-issue.yml`](../.github/workflows/comment-on-unready-assigned-issue.yml), implemented by [`comment_on_unready_assigned_issue.py`](../.github/scripts/comment_on_unready_assigned_issue.py), is not an agent workflow. It does not invoke Oz. It is a scripted guardrail that comments when an issue is assigned too early and removes the `oz-agent` assignee.

[`comment-on-ready-to-implement.yml`](../.github/workflows/comment-on-ready-to-implement.yml), [`comment-on-ready-to-spec.yml`](../.github/workflows/comment-on-ready-to-spec.yml), and [`comment-on-plan-approved.yml`](../.github/workflows/comment-on-plan-approved.yml) are also not agent workflows. They are inline `gh` CLI scripts that post a fixed reminder comment when the corresponding label is applied.

[`run-tests.yml`](../.github/workflows/run-tests.yml) is straightforward validation support in the PR pipeline; it runs the webhook + dispatcher tests under `tests/` and the helper unit tests under `.github/scripts/tests/`.

The `enforce-pr-issue-state` workflow that used to live in `.github/workflows/` has moved entirely into the webhook control plane. The synchronous allow/close decision runs inline in the webhook handler, and the cloud-agent fallback (`need-cloud-match`) is dispatched the same way as the other PR-triggered flows.

## Where workflows fit in

The reusable workflows are still the integration boundary for issue-triggered and plan-approval traffic. If another repository or orchestration layer wants to use those parts of the platform, it will normally call:

- [`triage-new-issues.yml`](../.github/workflows/triage-new-issues.yml)
- [`respond-to-triaged-issue-comment.yml`](../.github/workflows/respond-to-triaged-issue-comment.yml)
- [`create-spec-from-issue.yml`](../.github/workflows/create-spec-from-issue.yml)
- [`create-implementation-from-issue.yml`](../.github/workflows/create-implementation-from-issue.yml)
- [`comment-on-plan-approved.yml`](../.github/workflows/comment-on-plan-approved.yml)
- [`trigger-implementation-on-plan-approved.yml`](../.github/workflows/trigger-implementation-on-plan-approved.yml)
- [`remove-stale-issue-labels-on-plan-approved.yml`](../.github/workflows/remove-stale-issue-labels-on-plan-approved.yml)
- [`update-pr-review.yml`](../.github/workflows/update-pr-review.yml)
- [`update-triage.yml`](../.github/workflows/update-triage.yml)
- [`update-dedupe.yml`](../.github/workflows/update-dedupe.yml)

PR-triggered traffic (PR open, ready for review, `oz-review` label, `@oz-agent` mention, `/oz-review`, `/oz-verify`, PR `synchronize`/`edited`) is handled by the webhook control plane at the repo root. Consuming repositories do not need adapter YAMLs for those flows; they only need to point the GitHub App's webhook URL at their Vercel project.

The `*-local.yml` files in [`../.github/workflows/`](../.github/workflows/) — such as [`triage-new-issues-local.yml`](../.github/workflows/triage-new-issues-local.yml), [`create-spec-from-issue-local.yml`](../.github/workflows/create-spec-from-issue-local.yml), and [`create-implementation-from-issue-local.yml`](../.github/workflows/create-implementation-from-issue-local.yml) — are useful examples of one concrete wiring of the reusable layer onto GitHub events. They are still secondary to the skill-backed agent roles.

## In one sentence

`oz-for-oss` is a reusable OSS automation platform whose webhook control plane and GitHub Actions wrappers mainly exist to feed rich GitHub and repository context into a small set of skill-backed agent roles: triage, spec writing, implementation, review, and a family of narrowly scoped self-improvement loops that evolve repo-local companion skills.
