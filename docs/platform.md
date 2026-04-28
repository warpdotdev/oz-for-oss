# Platform workflows in `oz-for-oss`

The most useful way to understand this repository is not as a pile of workflows. It is as a small set of agent roles, each backed by repository-specific skills and prompts, with reusable workflows acting as the delivery mechanism that gets the right context in front of the right agent.

The reusable workflows in [`../.github/workflows/`](../.github/workflows/) are still the integration boundary for other systems. But the real behavior lives one layer down:

- skills in [`../.agents/skills/`](../.agents/skills/)
- prompt construction in the Python entrypoints under [`../.github/scripts/`](../.github/scripts/)
- shared Oz and GitHub helpers in [`../.github/scripts/oz_workflows/`](../.github/scripts/oz_workflows/)

That is the center of gravity for the platform. The workflows mostly decide when to run, what permissions to grant, and what repository state to pass in. The agents decide how to reason about that state.

## The agent model

This platform has a few recurring agent roles.

There is a triage agent that turns an issue into structured repository state. There are spec-writing agents that turn issue intent into durable planning artifacts. There is an implementation agent that turns approved intent into branch changes. There is a review agent that evaluates pull requests. And there is a self-improvement agent that updates the review instructions themselves based on reviewer feedback.

Those roles matter more than any single workflow file because the same role can appear in multiple entrypoints. A workflow can change, a trigger can move, or a local adapter can disappear, but the underlying agent role is what gives the system its shape.

GitHub is still the control plane. Issues, labels, assignees, comments, pull requests, and branches are the durable state. But the intelligence in the system comes from the prompts and skills that teach Oz how to read that state and what to do with it.

## How a workflow uses an agent

Most of the agent-backed workflows in this repo follow the same pattern:

1. a delivery surface decides that a unit of work should run — either a reusable workflow in [`../.github/workflows/`](../.github/workflows/), or the Vercel webhook receiver under [`../control-plane/`](../control-plane/)
2. a Python entrypoint in [`../.github/scripts/`](../.github/scripts/) gathers context from GitHub and the repository
3. that script assembles a task-specific prompt
4. the script invokes Oz against one or more local skills in [`../.agents/skills/`](../.agents/skills/)
5. the result is applied back to GitHub as labels, comments, branches, PRs, or reviews

For the triage and review workflows, the Vercel control plane stores the in-flight Oz run id in Vercel KV at dispatch time and a 1-minute cron tick handles the apply step — the webhook handler returns 202 immediately so GitHub never sees a long-running response. The agent uploads its result via `oz artifact upload <name>.json` and the cron-side workflow handler downloads it through `oz_workflows.artifacts.load_*_artifact`. The GitHub Actions workflows continue to work in parallel until the operator flips the GitHub App webhook URL.

That prompt-construction layer is important. The repo does not just say “run the review skill” or “run the triage skill.” It builds a concrete packet of issue or PR context around the skill:

- issue body and comments
- label taxonomy from [`config.json`](../.github/issue-triage/config.json)
- ownership hints from [`STAKEHOLDERS`](../.github/STAKEHOLDERS)
- spec context from [`../specs/`](../specs/)
- PR metadata, diff information, and prior review context

So the workflows are best read as orchestration wrappers around skill-backed prompts.

## The triage agent

The first major agent role is the triage agent. It is grounded in [`triage-issue`](../.agents/skills/triage-issue/SKILL.md) and supplemented by [`dedupe-issue`](../.agents/skills/dedupe-issue/SKILL.md). The main reusable workflow wrapper is [`triage-new-issues.yml`](../.github/workflows/triage-new-issues.yml), implemented by [`triage_new_issues.py`](../.github/scripts/triage_new_issues.py).

This agent’s job is not just to classify issues. Its real job is to turn a raw issue into something the rest of the system can trust. That means it has to separate observed symptoms from user hypotheses, normalize the issue body, apply managed labels, infer likely owners, check for duplicates, and decide whether more information is needed.

An important part of that role is clarification. In this platform, clarifying questions are part of triage rather than a separate afterthought. When an issue is underspecified, the triage agent is expected to ask the next useful questions and then re-triage once the reporter replies.

There is also a narrower follow-up path in [`respond-to-triaged-issue-comment.yml`](../.github/workflows/respond-to-triaged-issue-comment.yml), implemented by [`respond_to_triaged_issue_comment.py`](../.github/scripts/respond_to_triaged_issue_comment.py). That workflow still relies on the same analytical skill family, but it is operating in a more conversational mode: answer or interpret a follow-up comment on an already triaged issue without rerunning the full mutation path.

## The spec-writing agents

Once an issue is clear enough, the next important roles are the spec-writing agents. They are grounded in:

- [`spec-driven-implementation`](../.agents/skills/spec-driven-implementation/SKILL.md)
- [`create-product-spec`](../.agents/skills/create-product-spec/SKILL.md)
- [`create-tech-spec`](../.agents/skills/create-tech-spec/SKILL.md)
- [`write-product-spec`](../.agents/skills/write-product-spec/SKILL.md)
- [`write-tech-spec`](../.agents/skills/write-tech-spec/SKILL.md)

The main workflow wrapper is [`create-spec-from-issue.yml`](../.github/workflows/create-spec-from-issue.yml), implemented by [`create_spec_from_issue.py`](../.github/scripts/create_spec_from_issue.py).

These agents are doing something very specific: they translate issue discussion into durable repository artifacts under [`../specs/`](../specs/). The output is not just generated prose in a chat transcript. The output is committed planning state that other humans and agents can review, approve, and build against.

That is why the spec-writing role matters as its own abstraction. The workflow itself mostly handles assignment, branch creation, and PR creation after the agent has finished pushing the branch and handing back any PR metadata the workflow needs. The actual planning behavior — what belongs in a product spec, what belongs in a tech spec, how much to ground in existing code, how to distinguish user-facing behavior from implementation detail — comes from the skills.

## The implementation agent

After planning comes the implementation agent. It is grounded in:

- [`implement-issue`](../.agents/skills/implement-issue/SKILL.md)
- [`implement-specs`](../.agents/skills/implement-specs/SKILL.md)
- [`spec-driven-implementation`](../.agents/skills/spec-driven-implementation/SKILL.md)

The main reusable workflow wrapper is [`create-implementation-from-issue.yml`](../.github/workflows/create-implementation-from-issue.yml), implemented by [`create_implementation_from_issue.py`](../.github/scripts/create_implementation_from_issue.py).

This agent is not meant to freewheel off an issue description alone when stronger design context exists. The surrounding prompt assembly is explicit about using approved spec context when available, and about refusing to continue when spec PRs exist but are not yet approved. That constraint is not just workflow policy. It is part of the implementation agent’s contract with the rest of the system.

So the implementation role is best understood as: take approved intent, the current branch state, and repository validation expectations, then produce branch changes in the right place. The agent's job stops at the branch push plus any requested handoff artifacts; the workflow handles PR creation or refresh separately. The agent handles the reasoning that turns issue and spec context into code.

## The review agent

The review role is grounded in:

- [`review-pr`](../.agents/skills/review-pr/SKILL.md)
- [`review-spec`](../.agents/skills/review-spec/SKILL.md)
- [`check-impl-against-spec`](../.agents/skills/check-impl-against-spec/SKILL.md)

The main reusable wrapper is [`review-pull-request.yml`](../.github/workflows/review-pull-request.yml), implemented by [`review_pr.py`](../.github/scripts/review_pr.py).

The interesting part of the review role is not merely that it comments on PRs. It is that the repo already treats review as skill-specialized work. Spec-only PRs and code PRs are reviewed differently. Spec-aware review can pull in approved planning context. The prompt layer is careful about producing structured review output rather than free-form chat.

That makes the review agent less like a generic bot and more like a reusable evaluation role with multiple review modes. The workflow wrapper exists to fetch the right PR context, construct the prompt, and post the result back to GitHub.

There is also a repository-local branch-follow-up path in [`respond-to-pr-comment.yml`](../.github/workflows/respond-to-pr-comment.yml), implemented by [`respond_to_pr_comment.py`](../.github/scripts/respond_to_pr_comment.py). It is not part of the reusable `workflow_call` surface today, but it is still a useful illustration of the same core model: PR discussion becomes prompt context, and the implementation skill is reused to continue work on the branch.

## Core skills and repo-local companions

Each reusable agent role has a **core skill** in [`../.agents/skills/<agent>/SKILL.md`](../.agents/skills/) and, when repo-tunable behavior exists, a paired **repo-local companion** in [`../.agents/skills/<agent>-local/SKILL.md`](../.agents/skills/). The core skill expresses the cross-repo contract — output schema, severity labels, safety rules, evidence rules — and is treated as read-only from the self-improvement loops. The companion specializes only the override categories the core skill explicitly enumerates (for example `review-pr`'s user-facing-string norms, or `triage-issue`'s label taxonomy).

The initial companion skills are:

- [`review-pr-local`](../.agents/skills/review-pr-local/SKILL.md)
- [`review-spec-local`](../.agents/skills/review-spec-local/SKILL.md)
- [`triage-issue-local`](../.agents/skills/triage-issue-local/SKILL.md)
- [`dedupe-issue-local`](../.agents/skills/dedupe-issue-local/SKILL.md)

At prompt assembly time, the Python entrypoints in [`../.github/scripts/`](../.github/scripts/) call `resolve_repo_local_skill_path(workspace, core_skill_name)` to detect a companion in the consuming repository's workspace. When the file exists and contains non-frontmatter body content, the entrypoint appends a fenced "Repository-specific guidance" section to the prompt that *references* the companion path. The companion body is never inlined into the prompt; the agent reads the referenced file directly via its usual skill-read path. When no companion is present, the section is omitted entirely and the agent falls back to the core contract alone.

A consuming repository that has not ingested any repo-specific guidance yet can adopt `oz-for-oss` unchanged: the prompt-construction layer treats absent or effectively empty (frontmatter-only) companions as absent and runs the core skills with no special wiring. See [`bootstrap-issue-config`](../.agents/skills/bootstrap-issue-config/SKILL.md) for how new repositories scaffold empty companion skills during onboarding.

## The self-improvement agents

The last core role is self-improvement. Rather than a single loop, the platform ships a small family of loops, each scoped to a narrow repo-local companion:

- [`update-pr-review`](../.agents/skills/update-pr-review/SKILL.md) writes only to [`review-pr-local`](../.agents/skills/review-pr-local/SKILL.md) and [`review-spec-local`](../.agents/skills/review-spec-local/SKILL.md), implemented by [`update_pr_review.py`](../.github/scripts/update_pr_review.py) and driven by [`update-pr-review.yml`](../.github/workflows/update-pr-review.yml).
- [`update-triage`](../.agents/skills/update-triage/SKILL.md) writes only to [`triage-issue-local`](../.agents/skills/triage-issue-local/SKILL.md) and [`.github/issue-triage/*`](../.github/issue-triage/), implemented by [`update_triage.py`](../.github/scripts/update_triage.py) and driven by [`update-triage.yml`](../.github/workflows/update-triage.yml).
- [`update-dedupe`](../.agents/skills/update-dedupe/SKILL.md) writes only to [`dedupe-issue-local`](../.agents/skills/dedupe-issue-local/SKILL.md), implemented by [`update_dedupe.py`](../.github/scripts/update_dedupe.py) and driven by [`update-dedupe.yml`](../.github/workflows/update-dedupe.yml).

These loops do not review application code or specs directly. Instead, each one reads a narrow signal (PR review feedback, maintainer triage overrides, closed-as-duplicate events) and proposes minimum-viable edits to the corresponding companion skill. The Python entrypoint gates the push behind a `git diff` guard: any change outside the declared write surface aborts the run before a PR is opened. The core skill files and the workflow scripts are never writable from a self-improvement loop.

That is why this part of the platform is best described as a family of self-improvement loops. The repo is not only using agent skills — it is curating and evolving each repo-local companion based on observed signal, while keeping the cross-repo contracts stable.

## The workflows that are not really agents

Looking at the platform through agent roles also makes it clearer which pieces are not agents at all.

[`comment-on-unready-assigned-issue.yml`](../.github/workflows/comment-on-unready-assigned-issue.yml), implemented by [`comment_on_unready_assigned_issue.py`](../.github/scripts/comment_on_unready_assigned_issue.py), is not an agent workflow. It does not invoke Oz. It is a scripted guardrail that comments when an issue is assigned too early and removes the `oz-agent` assignee.

[`run-tests.yml`](../.github/workflows/run-tests.yml) is also not an agent workflow. It is straightforward validation support in the PR pipeline.

[`enforce-pr-issue-state.yml`](../.github/workflows/enforce-pr-issue-state.yml), implemented by [`enforce_pr_issue_state.py`](../.github/scripts/enforce_pr_issue_state.py), sits in between. It is not primarily an agent role of its own. It is a policy gate that only falls back to Oz when fuzzy association work is needed. In other words, it is best thought of as orchestration and enforcement with conditional agent help, not as a first-class reasoning role like triage, implementation, or review.

## Where workflows fit in

The reusable workflows are still the integration boundary for other systems. If another repository or orchestration layer wants to use this platform, it will normally call:

- [`triage-new-issues.yml`](../.github/workflows/triage-new-issues.yml)
- [`respond-to-triaged-issue-comment.yml`](../.github/workflows/respond-to-triaged-issue-comment.yml)
- [`create-spec-from-issue.yml`](../.github/workflows/create-spec-from-issue.yml)
- [`create-implementation-from-issue.yml`](../.github/workflows/create-implementation-from-issue.yml)
- [`review-pull-request.yml`](../.github/workflows/review-pull-request.yml)
- [`update-pr-review.yml`](../.github/workflows/update-pr-review.yml)
- [`update-triage.yml`](../.github/workflows/update-triage.yml)
- [`update-dedupe.yml`](../.github/workflows/update-dedupe.yml)

But those workflows make more sense once you see them as wrappers around the agent roles above. They decide when to run, what secrets and permissions to grant, and what repository context to package. The agent skills and prompts are what give the system its actual behavior.

The local adapters in [`../.github/workflows/`](../.github/workflows/) — such as [`triage-new-issues-local.yml`](../.github/workflows/triage-new-issues-local.yml), [`create-spec-from-issue-local.yml`](../.github/workflows/create-spec-from-issue-local.yml), [`create-implementation-from-issue-local.yml`](../.github/workflows/create-implementation-from-issue-local.yml), and [`pr-hooks.yml`](../.github/workflows/pr-hooks.yml) — are useful examples of one concrete wiring of that reusable layer onto GitHub events. They are still secondary to the skill-backed agent roles.

## In one sentence

`oz-for-oss` is a reusable OSS automation platform whose workflows mainly exist to feed rich GitHub and repository context into a small set of skill-backed agent roles: triage, spec writing, implementation, review, and a family of narrowly scoped self-improvement loops that evolve repo-local companion skills.
