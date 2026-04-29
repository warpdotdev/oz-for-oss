# oz-for-oss

Oz for OSS is a reusable open-source automation platform that lets a Warp-hosted Oz agent triage issues, draft product and tech specs, open implementation PRs, review pull requests, respond to PR comments, and verify changes via slash commands. The intelligence in the system lives in the agent skills under [`.agents/skills/`](.agents/skills/) and in the prompt-construction layer that feeds them concrete repository context. Everything else in the repo is delivery wiring around those skills.

This branch ships two complementary delivery surfaces:

- **A Vercel-hosted webhook control plane at the repo root** — `api/`, `lib/`, `tests/`, and `vercel.json` together implement a GitHub webhook receiver plus a 1-minute cron poller. The webhook is the sole delivery surface for **PR-triggered** bot behavior (`review-pull-request`, `enforce-pr-issue-state`, `respond-to-pr-comment`, `verify-pr-comment`).
- **GitHub Actions workflows under [`.github/workflows/`](.github/workflows/)** — these continue to handle **issue-triggered** workflows (triage, spec creation, implementation, ready-label comments, unready-assignment guard) and the **plan-approval** workflows (`comment-on-plan-approved`, `trigger-implementation-on-plan-approved`, `remove-stale-issue-labels-on-plan-approved`). Those workflows clone the repository, push branches, and open PRs, which lines up better with a long-running Actions runner than with a fire-and-forget cloud agent dispatch. Finally, the scheduled self-improvement loops (`update-pr-review`, `update-triage`, `update-dedupe`) run as Actions on a weekly cron and the [`run-tests.yml`](.github/workflows/run-tests.yml) workflow runs the repo's own CI on every PR.

Triage label definitions live in [`.github/issue-triage/config.json`](.github/issue-triage/config.json). The CODEOWNERS-style stakeholder map lives in [`.github/STAKEHOLDERS`](.github/STAKEHOLDERS). Committed spec artifacts live under [`specs/GH{number}/product.md`](specs/) and [`specs/GH{number}/tech.md`](specs/).

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
│   │   ├── respond-to-triaged-issue-comment{,-local}.yml   # issue_comment on issues
│   │   ├── triage-new-issues{,-local}.yml                  # issues
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
│   │   ├── triage_new_issues.py
│   │   ├── trigger_implementation_on_plan_approved.py
│   │   ├── update_dedupe.py
│   │   ├── update_pr_review.py
│   │   ├── update_triage.py
│   │   └── tests/                  # Unit tests for helpers + GHA scripts
│   ├── STAKEHOLDERS                # CODEOWNERS-style stakeholder map
│   ├── issue-triage/config.json    # Triage label taxonomy
│   └── oz/config.yml               # Bundled fallback Oz config
├── docs/
│   └── platform.md
├── specs/                          # Approved product + tech specs
└── CONTRIBUTING.md
```

The webhook ships every file under `lib/` to Vercel as part of the function bundle, so `from lib.X import …` resolves both at runtime and in tests. The legacy GitHub Actions wrappers under `.github/workflows/` use the `run-oz-python-script` composite action, which adds `lib/` and `.github/scripts/` to `PYTHONPATH` so the same `oz_workflows` package powers both surfaces.

## How a webhook-driven workflow runs

Every PR-triggered flow follows the same sequence:

1. **GitHub delivers a webhook** for `pull_request`, `pull_request_review_comment`, or PR-conversation `issue_comment` events to `https://<vercel-project>.vercel.app/api/webhook`.
2. **Signature verification.** [`lib/signatures.py`](lib/signatures.py) verifies the `X-Hub-Signature-256` header against the shared `OZ_GITHUB_WEBHOOK_SECRET`.
3. **Routing.** [`lib/routing.py`](lib/routing.py) maps the event onto one of `review-pull-request`, `enforce-pr-issue-state`, `respond-to-pr-comment`, or `verify-pr-comment`. Plain `issues` events and plain `issue_comment` events on issues (without `pull_request`) are deliberately dropped — those are the GitHub Actions workflows' responsibility.
4. **Synchronous decisions.** `enforce-pr-issue-state` runs the deterministic allow/close decision inline so the legacy latency profile is preserved. Only the `need-cloud-match` branch falls through to the cloud agent.
5. **Prompt construction + dispatch.** [`lib/builders.py`](lib/builders.py) gathers PR context from PyGithub, posts the workflow's `progress.start(...)` comment, and stashes the resulting `progress_comment_id` on the `DispatchRequest.payload_subset`. [`lib/dispatch.py`](lib/dispatch.py) then calls the Oz Python SDK to start the cloud agent run and persists an in-flight record in Vercel KV.
6. **202 response.** The webhook returns `202 Accepted` within ~100 ms, well inside Vercel's per-request budget. The GitHub Recent Deliveries UI stays green.
7. **Cron drain.** [`api/cron.py`](api/cron.py) runs every minute. For each in-flight run it:
   - Polls the Oz API for run state.
   - On a non-terminal poll, refreshes the progress comment with the live Warp session link via the per-workflow `non_terminal_handler`.
   - On `SUCCEEDED`, calls the workflow's artifact loader (`oz_workflows.artifacts.load_*_artifact`) and result applier (`scripts.<workflow>.apply_*_result`) to write the outcome back to GitHub.
   - On `FAILED`/`ERROR`/`CANCELLED`, calls the workflow's `failure_handler` to update the progress comment with an error.

The progress comment posted at dispatch is the same comment edited on every cron tick — `WorkflowProgressComment` is reconstructed cron-side from the stashed `progress_comment_id`.

## How the GitHub Actions workflows run

Issue-triggered and plan-approval workflows are still expressed as ordinary GitHub Actions workflows. They run inside an Ubuntu runner so they can clone the repo, run shell tools, push branches, and open PRs the way the legacy implementation always did. Each workflow's responsibilities, triggers, and supporting prompt code are listed below.

| Trigger | Workflow YAML | Python entrypoint |
|---|---|---|
| `issues: [opened, reopened, edited, labeled]`, `issue_comment: [created]` | [`triage-new-issues-local.yml`](.github/workflows/triage-new-issues-local.yml) → [`triage-new-issues.yml`](.github/workflows/triage-new-issues.yml) | [`triage_new_issues.py`](.github/scripts/triage_new_issues.py) |
| `issue_comment: [created]` on triaged issues | [`respond-to-triaged-issue-comment-local.yml`](.github/workflows/respond-to-triaged-issue-comment-local.yml) → [`respond-to-triaged-issue-comment.yml`](.github/workflows/respond-to-triaged-issue-comment.yml) | [`respond_to_triaged_issue_comment.py`](.github/scripts/respond_to_triaged_issue_comment.py) |
| `issues: [assigned, labeled]`, `issue_comment: [created]` (with `ready-to-spec`) | [`create-spec-from-issue-local.yml`](.github/workflows/create-spec-from-issue-local.yml) → [`create-spec-from-issue.yml`](.github/workflows/create-spec-from-issue.yml) | [`create_spec_from_issue.py`](.github/scripts/create_spec_from_issue.py) |
| `issues: [assigned, labeled]`, `issue_comment: [created]` (with `ready-to-implement`) | [`create-implementation-from-issue-local.yml`](.github/workflows/create-implementation-from-issue-local.yml) → [`create-implementation-from-issue.yml`](.github/workflows/create-implementation-from-issue.yml) | [`create_implementation_from_issue.py`](.github/scripts/create_implementation_from_issue.py) |
| `issues: [assigned]` without `ready-*` labels | [`comment-on-unready-assigned-issue-local.yml`](.github/workflows/comment-on-unready-assigned-issue-local.yml) → [`comment-on-unready-assigned-issue.yml`](.github/workflows/comment-on-unready-assigned-issue.yml) | [`comment_on_unready_assigned_issue.py`](.github/scripts/comment_on_unready_assigned_issue.py) |
| `issues: [labeled]` (`ready-to-spec`) | [`comment-on-ready-to-spec.yml`](.github/workflows/comment-on-ready-to-spec.yml) | (gh CLI inline) |
| `issues: [labeled]` (`ready-to-implement`) | [`comment-on-ready-to-implement.yml`](.github/workflows/comment-on-ready-to-implement.yml) | (gh CLI inline) |
| `pull_request_target: [labeled]` (`plan-approved`) | [`comment-on-plan-approved.yml`](.github/workflows/comment-on-plan-approved.yml) | (gh CLI inline) |
| `pull_request_target: [labeled]` (`plan-approved`) | [`trigger-implementation-on-plan-approved-local.yml`](.github/workflows/trigger-implementation-on-plan-approved-local.yml) → [`trigger-implementation-on-plan-approved.yml`](.github/workflows/trigger-implementation-on-plan-approved.yml) | [`trigger_implementation_on_plan_approved.py`](.github/scripts/trigger_implementation_on_plan_approved.py) |
| `pull_request_target: [labeled]` (`plan-approved`) | [`remove-stale-issue-labels-on-plan-approved-local.yml`](.github/workflows/remove-stale-issue-labels-on-plan-approved-local.yml) → [`remove-stale-issue-labels-on-plan-approved.yml`](.github/workflows/remove-stale-issue-labels-on-plan-approved.yml) | [`remove_stale_issue_labels_on_plan_approved.py`](.github/scripts/remove_stale_issue_labels_on_plan_approved.py) |
| Weekly cron | [`update-{dedupe,pr-review,triage}{,-local}.yml`](.github/workflows/) | [`update_dedupe.py`](.github/scripts/update_dedupe.py), [`update_pr_review.py`](.github/scripts/update_pr_review.py), [`update_triage.py`](.github/scripts/update_triage.py) |
| `pull_request` (open/sync/reopen/ready_for_review) on this repo | [`run-tests.yml`](.github/workflows/run-tests.yml) | unittest + pytest |

The `*-local.yml` files are the thin local adapters that map GitHub events to the reusable `workflow_call` workflow. Consuming repos copy the `*-local.yml` files into their own `.github/workflows/` and change the `uses:` reference to point at `warpdotdev/oz-for-oss/.github/workflows/<workflow>.yml@main`.

## Have an open-source project?

Actively maintained open-source projects can apply for the [Oz Open Source Partnership](https://docs.warp.dev/support-and-community/community/open-source-partnership) to receive free Oz credits for using these workflows. Accepted projects can use Oz agents for tasks like issue triage, pull request review, documentation, and implementation support across their repositories.

To apply, [fill out the application form](https://tally.so/r/LZWxqG) and we'll be in touch.

## How to use these workflows in your own repo

Because the bot has two delivery surfaces, onboarding into your own repo has two parts.

### 1. Set up the GitHub App

The webhook handler authenticates as a GitHub App and so do the issue-triggered Actions. Create the App (organization-owned or user-owned), grant it these permissions, and install it on every repository that should receive the bot:

**Repository permissions**

- **Contents** — Read & Write (checkout code, push branches)
- **Issues** — Read & Write (apply labels, post comments, manage assignees)
- **Pull requests** — Read & Write (open PRs, post reviews)

**Webhook events**

- `issues`, `issue_comment`, `pull_request`, `pull_request_review_comment`

Note the **App ID** and a generated **private key** — both are needed for the GitHub Actions secrets and the Vercel project secrets.

### 2. Provision the Vercel webhook control plane

```sh
# From the root of this repo (or your fork)
vercel link
vercel deploy
```

`vercel.json` declares the `api/webhook.py` and `api/cron.py` functions plus the 1-minute cron schedule. Set the project's secrets through the Vercel dashboard:

| Secret / variable | Description |
|---|---|
| `OZ_GITHUB_WEBHOOK_SECRET` | Shared HMAC secret configured on the GitHub App's webhook delivery. |
| `OZ_GITHUB_APP_ID` | Numeric App ID. |
| `OZ_GITHUB_APP_PRIVATE_KEY` | PEM-encoded App private key. |
| `WARP_API_KEY` | Warp API key used to dispatch Oz cloud agents. |
| `WARP_API_BASE_URL` | Defaults to `https://app.warp.dev/api/v1`. Override for staging. |
| `WARP_ENVIRONMENT_ID` | Default Oz cloud environment UID. |
| `WARP_REVIEW_TRIAGE_ENVIRONMENT_ID` | Optional override used by review/triage runs. Falls back to `WARP_ENVIRONMENT_ID` when empty. |
| `CRON_SECRET` | Random secret used to authenticate Vercel cron requests. |
| `GITHUB_API_BASE_URL` | Optional. Defaults to `https://api.github.com`. Override for GitHub Enterprise. |

Provision a Vercel KV resource on the project. Vercel injects `KV_REST_API_URL` / `KV_REST_API_TOKEN` automatically; the cron handler reads them at runtime through `upstash-redis`.

Finally, point the GitHub App's webhook URL at `https://<vercel-project>.vercel.app/api/webhook`. The webhook handler returns `202` for every delivery so the App's "Recent deliveries" UI stays green even when the cron tick is busy.

### 3. Configure GitHub Actions secrets and variables

The issue-triggered and self-improvement workflows still authenticate through the same App. Add these **secrets** to the consuming repository (or to the org so multiple repos can share them):

| Secret | Description |
|---|---|
| `OZ_MGMT_GHA_APP_ID` | Numeric App ID. |
| `OZ_MGMT_GHA_PRIVATE_KEY` | PEM-encoded private key. |
| `OSS_WARP_API_KEY` | Warp API key used to dispatch cloud agents from inside the runner. |

Set the following **repository variables** (not secrets):

| Variable | Description |
|---|---|
| `WARP_ENVIRONMENT_ID` | **Required.** Oz cloud environment UID for spec / implementation / response runs. |
| `WARP_AGENT_MODEL` | Optional. Override the default Oz model. |
| `WARP_REVIEW_TRIAGE_ENVIRONMENT_ID` | Optional. Dedicated environment for triage runs. Falls back to `WARP_ENVIRONMENT_ID`. |

### 4. Add local adapter workflows

For each issue-triggered or plan-approval flow you want, copy the matching `*-local.yml` file into your repository's `.github/workflows/` and update the `uses:` reference from `./.github/workflows/<workflow>.yml` to `warpdotdev/oz-for-oss/.github/workflows/<workflow>.yml@main`. PR-triggered flows (`review-pull-request`, `enforce-pr-issue-state`, `respond-to-pr-comment`, `verify-pr-comment`) do **not** need adapter YAMLs anymore — the webhook control plane handles them as soon as the GitHub App's webhook URL is wired to your Vercel project.

### 5. Configure shared Oz workflow settings (optional)

Repositories can commit `.github/oz/config.yml` to make workflow-level defaults visible and reviewable in source control. Oz resolves that file from the consuming repository first and falls back to the bundled [`.github/oz/config.yml`](.github/oz/config.yml) when absent. Discovery stops at the first existing file — the two locations are not merged. The settings live under `self_improvement` and `triage`:

```yaml
version: 1
self_improvement:
  reviewers:
    - octocat
    - repo-maintainer
  base_branch: auto
triage:
  prior_triage_labels:
    - triaged
```

### 6. Bootstrap triage configuration (optional)

Run the [`bootstrap-issue-config`](.agents/skills/bootstrap-issue-config/SKILL.md) skill against your repository to seed `.github/issue-triage/config.json` and `.github/STAKEHOLDERS` with sensible defaults derived from your existing labels and CODEOWNERS.

## Local development

### Set up the Python env

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Run the test suites

The webhook + dispatcher tests:

```sh
python -m pytest tests
```

The shared helpers + self-improvement entrypoint tests (the ones that still ship with the `.github/scripts/` files):

```sh
PYTHONPATH=lib:.github/scripts python -m unittest discover -s .github/scripts/tests
```

Both suites run in `run-tests.yml` on every PR.

### Run the webhook locally

```sh
cd /path/to/oz-for-oss
vercel dev
```

`vercel dev` boots `api/webhook.py` and `api/cron.py` behind a local HTTP server. Replay a synthetic delivery against the local endpoint by signing the body with the same `OZ_GITHUB_WEBHOOK_SECRET` Vercel uses:

```sh
BODY='{"action":"opened","pull_request":{"number":42,"state":"open","draft":false,"user":{"login":"alice","type":"User"}},"repository":{"full_name":"acme/widgets"},"installation":{"id":1234}}'
SECRET="$OZ_GITHUB_WEBHOOK_SECRET"
SIGNATURE="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
curl -sS -X POST http://localhost:3000/api/webhook \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: $SIGNATURE" \
  --data "$BODY"
```

The handler returns `202` with the routed workflow id (or `null` when the event is intentionally dropped).

### Run a GitHub Actions entrypoint locally

The scripts under `.github/scripts/` expect the same environment variables the workflow YAMLs hand them. To debug locally:

```sh
PYTHONPATH=lib:.github/scripts \
  GH_TOKEN=$(gh auth token) \
  WARP_API_KEY=... \
  WARP_ENVIRONMENT_ID=... \
  GITHUB_REPOSITORY=acme/widgets \
  GITHUB_EVENT_PATH=$(pwd)/event.json \
  GITHUB_RUN_ID=local-run \
  python .github/scripts/triage_new_issues.py
```
