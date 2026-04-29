# Architecture

`oz-for-oss` ships two complementary delivery surfaces around the same shared Python helpers and skills:

- **A Vercel-hosted webhook control plane at the repo root** — `api/`, `lib/`, `tests/`, and `vercel.json` together implement a GitHub webhook receiver plus a 1-minute cron poller. The webhook is the sole delivery surface for **PR-triggered** bot behavior (`review-pull-request`, `enforce-pr-issue-state`, `respond-to-pr-comment`, `verify-pr-comment`) and for the **issue-triage** flow (`triage-new-issues`, including `@oz-agent` mentions on non-triaged issues and `needs-info` reporter replies).
- **GitHub Actions workflows under [`../.github/workflows/`](../.github/workflows/)** — these continue to handle the remaining **issue-triggered** flows (`respond-to-triaged-issue-comment`, spec creation, implementation, ready-label comments, unready-assignment guard) and the **plan-approval** workflows (`comment-on-plan-approved`, `trigger-implementation-on-plan-approved`, `remove-stale-issue-labels-on-plan-approved`). Those workflows clone the repository, push branches, and open PRs, which lines up better with a long-running Actions runner than with a fire-and-forget cloud agent dispatch. Finally, the scheduled self-improvement loops (`update-pr-review`, `update-triage`, `update-dedupe`) run as Actions on a weekly cron and the [`run-tests.yml`](../.github/workflows/run-tests.yml) workflow runs the repo's own CI on every PR.

Triage label definitions live in [`../.github/issue-triage/config.json`](../.github/issue-triage/config.json). The CODEOWNERS-style stakeholder map lives in [`../.github/STAKEHOLDERS`](../.github/STAKEHOLDERS). Committed spec artifacts live under [`../specs/GH{number}/product.md`](../specs/) and [`../specs/GH{number}/tech.md`](../specs/).

## Repository layout

```
.
├── api/                          # Vercel serverless entrypoints
│   ├── webhook.py                # POST /api/webhook
│   └── cron.py                   # GET  /api/cron (1 minute schedule)
├── lib/                          # Shared webhook + helper code
│   ├── builders.py               # Per-workflow prompt builders (cloud dispatch)
│   ├── dispatch.py               # Oz cloud agent dispatcher
│   ├── handlers.py               # Cron-side artifact loaders + result appliers
│   ├── poll_runs.py              # Cron drain loop
│   ├── routing.py                # Webhook event → workflow router
│   ├── signatures.py             # X-Hub-Signature-256 verification
│   ├── state.py                  # Vercel KV in-flight run state
│   ├── trust.py                  # Org-membership trust evaluation
│   ├── github_app.py             # App installation token minting
│   ├── oz_workflows/             # Shared Oz/GitHub helpers (canonical)
│   └── scripts/                  # Workflow-specific gather/build/apply helpers
├── tests/                        # Webhook + dispatcher unit tests
├── vercel.json                   # Vercel function + cron config
├── requirements.txt              # Python deps (webhook + GH Actions)
├── .agents/skills/               # Agent skills (read by prompts)
├── .github/
│   ├── workflows/
│   │   ├── run-tests.yml                                   # Repo CI on pull_request
│   │   ├── comment-on-plan-approved.yml                    # PR labeled plan-approved
│   │   ├── comment-on-ready-to-implement.yml               # issues labeled
│   │   ├── comment-on-ready-to-spec.yml                    # issues labeled
│   │   ├── comment-on-unready-assigned-issue{,-local}.yml  # issues assigned
│   │   ├── create-implementation-from-issue{,-local}.yml   # issues + comments
│   │   ├── create-spec-from-issue{,-local}.yml             # issues + comments
│   │   ├── remove-stale-issue-labels-on-plan-approved{,-local}.yml
│   │   ├── respond-to-triaged-issue-comment{,-local}.yml   # issue_comment on triaged issues
│   │   ├── trigger-implementation-on-plan-approved{,-local}.yml
│   │   ├── update-{dedupe,pr-review,triage}{,-local}.yml   # weekly cron
│   ├── actions/
│   │   ├── run-oz-python-script/   # Used by the issue-triggered + update workflows
│   │   └── setup-oz-python/        # Shared uv setup used by run-tests
│   ├── scripts/                    # Issue-triggered + self-improvement entrypoints
│   │   ├── comment_on_unready_assigned_issue.py
│   │   ├── create_implementation_from_issue.py
│   │   ├── create_spec_from_issue.py
│   │   ├── remove_stale_issue_labels_on_plan_approved.py
│   │   ├── respond_to_triaged_issue_comment.py
│   │   ├── trigger_implementation_on_plan_approved.py
│   │   ├── update_dedupe.py
│   │   ├── update_pr_review.py
│   │   ├── update_triage.py
│   │   └── tests/                  # Unit tests for helpers + GHA scripts
│   ├── STAKEHOLDERS                # CODEOWNERS-style stakeholder map
│   ├── issue-triage/config.json    # Triage label taxonomy
│   └── oz/config.yml               # Bundled fallback Oz config
├── docs/
│   ├── architecture.md
│   ├── onboarding.md
│   └── platform.md
├── specs/                          # Approved product + tech specs
└── CONTRIBUTING.md
```

The webhook ships every file under `lib/` to Vercel as part of the function bundle, so `from lib.X import …` resolves both at runtime and in tests. The legacy GitHub Actions wrappers under `../.github/workflows/` use the `run-oz-python-script` composite action, which adds `lib/` and `.github/scripts/` to `PYTHONPATH` so the same `oz_workflows` package powers both surfaces.

## How a webhook-driven workflow runs

Every webhook-driven flow follows the same sequence:

1. **GitHub delivers a webhook** for `pull_request`, `pull_request_review_comment`, `issues`, or `issue_comment` events to `https://<vercel-project>.vercel.app/api/webhook`.
2. **Signature verification.** [`../lib/signatures.py`](../lib/signatures.py) verifies the `X-Hub-Signature-256` header against the shared `OZ_GITHUB_WEBHOOK_SECRET`.
3. **Routing.** [`../lib/routing.py`](../lib/routing.py) maps the event onto one of `review-pull-request`, `enforce-pr-issue-state`, `respond-to-pr-comment`, `verify-pr-comment`, or `triage-new-issues`. `@oz-agent` mentions on already-triaged issues stay on the GitHub Actions delivery path (`respond-to-triaged-issue-comment`); other plain-issue traffic that does not match a triage trigger is dropped with a structured reason.
4. **Synchronous decisions.** `enforce-pr-issue-state` runs the deterministic allow/close decision inline so the legacy latency profile is preserved. Only the `need-cloud-match` branch falls through to the cloud agent.
5. **Prompt construction + dispatch.** [`../lib/builders.py`](../lib/builders.py) gathers PR context from PyGithub, posts the workflow's `progress.start(...)` comment, and stashes the resulting `progress_comment_id` on the `DispatchRequest.payload_subset`. [`../lib/dispatch.py`](../lib/dispatch.py) then calls the Oz Python SDK to start the cloud agent run and persists an in-flight record in Vercel KV.
6. **202 response.** The webhook returns `202 Accepted` within ~100 ms, well inside Vercel's per-request budget. The GitHub Recent Deliveries UI stays green.
7. **Cron drain.** [`../api/cron.py`](../api/cron.py) runs every minute. For each in-flight run it:
   - Polls the Oz API for run state.
   - On a non-terminal poll, refreshes the progress comment with the live Warp session link via the per-workflow `non_terminal_handler`.
   - On `SUCCEEDED`, calls the workflow's artifact loader (`oz_workflows.artifacts.load_*_artifact`) and result applier (`scripts.<workflow>.apply_*_result`) to write the outcome back to GitHub.
   - On `FAILED`/`ERROR`/`CANCELLED`, calls the workflow's `failure_handler` to update the progress comment with an error.

The progress comment posted at dispatch is the same comment edited on every cron tick — `WorkflowProgressComment` is reconstructed cron-side from the stashed `progress_comment_id`.

## How the GitHub Actions workflows run

Issue-triggered and plan-approval workflows are still expressed as ordinary GitHub Actions workflows. They run inside an Ubuntu runner so they can clone the repo, run shell tools, push branches, and open PRs the way the legacy implementation always did. Each workflow's responsibilities, triggers, and supporting prompt code are listed below.

| Trigger | Workflow YAML | Python entrypoint |
|---|---|---|
| `issue_comment: [created]` on triaged issues | [`respond-to-triaged-issue-comment-local.yml`](../.github/workflows/respond-to-triaged-issue-comment-local.yml) → [`respond-to-triaged-issue-comment.yml`](../.github/workflows/respond-to-triaged-issue-comment.yml) | [`respond_to_triaged_issue_comment.py`](../.github/scripts/respond_to_triaged_issue_comment.py) |
| `issues: [assigned, labeled]`, `issue_comment: [created]` (with `ready-to-spec`) | [`create-spec-from-issue-local.yml`](../.github/workflows/create-spec-from-issue-local.yml) → [`create-spec-from-issue.yml`](../.github/workflows/create-spec-from-issue.yml) | [`create_spec_from_issue.py`](../.github/scripts/create_spec_from_issue.py) |
| `issues: [assigned, labeled]`, `issue_comment: [created]` (with `ready-to-implement`) | [`create-implementation-from-issue-local.yml`](../.github/workflows/create-implementation-from-issue-local.yml) → [`create-implementation-from-issue.yml`](../.github/workflows/create-implementation-from-issue.yml) | [`create_implementation_from_issue.py`](../.github/scripts/create_implementation_from_issue.py) |
| `issues: [assigned]` without `ready-*` labels | [`comment-on-unready-assigned-issue-local.yml`](../.github/workflows/comment-on-unready-assigned-issue-local.yml) → [`comment-on-unready-assigned-issue.yml`](../.github/workflows/comment-on-unready-assigned-issue.yml) | [`comment_on_unready_assigned_issue.py`](../.github/scripts/comment_on_unready_assigned_issue.py) |
| `issues: [labeled]` (`ready-to-spec`) | [`comment-on-ready-to-spec.yml`](../.github/workflows/comment-on-ready-to-spec.yml) | (gh CLI inline) |
| `issues: [labeled]` (`ready-to-implement`) | [`comment-on-ready-to-implement.yml`](../.github/workflows/comment-on-ready-to-implement.yml) | (gh CLI inline) |
| `pull_request_target: [labeled]` (`plan-approved`) | [`comment-on-plan-approved.yml`](../.github/workflows/comment-on-plan-approved.yml) | (gh CLI inline) |
| `pull_request_target: [labeled]` (`plan-approved`) | [`trigger-implementation-on-plan-approved-local.yml`](../.github/workflows/trigger-implementation-on-plan-approved-local.yml) → [`trigger-implementation-on-plan-approved.yml`](../.github/workflows/trigger-implementation-on-plan-approved.yml) | [`trigger_implementation_on_plan_approved.py`](../.github/scripts/trigger_implementation_on_plan_approved.py) |
| `pull_request_target: [labeled]` (`plan-approved`) | [`remove-stale-issue-labels-on-plan-approved-local.yml`](../.github/workflows/remove-stale-issue-labels-on-plan-approved-local.yml) → [`remove-stale-issue-labels-on-plan-approved.yml`](../.github/workflows/remove-stale-issue-labels-on-plan-approved.yml) | [`remove_stale_issue_labels_on_plan_approved.py`](../.github/scripts/remove_stale_issue_labels_on_plan_approved.py) |
| Weekly cron | [`update-{dedupe,pr-review,triage}{,-local}.yml`](../.github/workflows/) | [`update_dedupe.py`](../.github/scripts/update_dedupe.py), [`update_pr_review.py`](../.github/scripts/update_pr_review.py), [`update_triage.py`](../.github/scripts/update_triage.py) |
| `pull_request` (open/sync/reopen/ready_for_review) on this repo | [`run-tests.yml`](../.github/workflows/run-tests.yml) | unittest + pytest |

The `*-local.yml` files are the thin local adapters that map GitHub events to the reusable `workflow_call` workflow. Consuming repos copy the `*-local.yml` files into their own `.github/workflows/` and change the `uses:` reference to point at `warpdotdev/oz-for-oss/.github/workflows/<workflow>.yml@main`.
