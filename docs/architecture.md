# Architecture

`oz-for-oss` now uses a single delivery surface for agent-backed behavior: the Vercel-hosted webhook control plane at the repo root. `api/`, `core/`, `tests/`, and `vercel.json` implement the GitHub webhook receiver, Oz run dispatch, Vercel KV state storage, and cron poller that applies completed agent results back to GitHub.

GitHub Actions is intentionally limited to repository CI via [`../.github/workflows/run-tests.yml`](../.github/workflows/run-tests.yml). The older reusable Actions wrappers and `.github/scripts` entrypoints were removed so webhook dispatch is the only bot runtime.

Triage label definitions live in [`../.github/issue-triage/config.json`](../.github/issue-triage/config.json). The CODEOWNERS-style stakeholder map lives in [`../.github/STAKEHOLDERS`](../.github/STAKEHOLDERS). The bundled fallback Oz workflow config lives in [`../.github/oz/config.yml`](../.github/oz/config.yml). Committed spec artifacts live under [`../specs/GH{number}/product.md`](../specs/) and [`../specs/GH{number}/tech.md`](../specs/).

## Repository layout

```
.
├── api/                          # Vercel serverless entrypoints
│   ├── webhook.py                # POST /api/webhook
│   └── cron.py                   # GET  /api/cron (1 minute schedule)
├── core/                          # Shared webhook + helper code
│   ├── builders.py               # Public builder registry
│   ├── dispatch.py               # Oz cloud-agent dispatcher
│   ├── handlers.py               # Public cron handler registry
│   ├── poll_runs.py              # Cron drain loop
│   ├── routing.py                # Webhook event → workflow router
│   ├── workflow_adapters.py      # AgentWorkflow → dispatch/handler adapters
│   ├── workflows/                # Concrete workflow classes
│   ├── scripts/                  # Workflow-specific gather/build/apply helpers
│   └── oz/             # Shared Oz/GitHub helpers
├── tests/                        # Webhook + dispatcher unit tests
├── vercel.json                   # Vercel function + cron config
├── requirements.txt              # Python deps for Vercel + tests
├── .agents/skills/               # Agent skills read by prompts
├── .github/
│   ├── workflows/run-tests.yml   # Repository CI only
│   ├── STAKEHOLDERS              # CODEOWNERS-style stakeholder map
│   ├── issue-triage/config.json  # Triage label taxonomy
│   └── oz/config.yml             # Bundled fallback Oz config
├── docs/
├── specs/                        # Approved product + tech specs
└── CONTRIBUTING.md
```

## How a webhook-driven workflow runs

Every agent-backed flow follows the same sequence:

1. **GitHub delivers a webhook** for `pull_request`, `pull_request_review_comment`, `issues`, or `issue_comment` events to `https://<vercel-project>.vercel.app/api/webhook`.
2. **Signature verification.** [`../core/signatures.py`](../core/signatures.py) verifies the `X-Hub-Signature-256` header against `OZ_GITHUB_WEBHOOK_SECRET`.
3. **Routing.** [`../core/routing.py`](../core/routing.py) maps the event to a workflow such as `review-pull-request`, `respond-to-pr-comment`, `verify-pr-comment`, `triage-new-issues`, `create-spec-from-issue`, `create-implementation-from-issue`, `plan-approved`, or `announce-ready-issue`.
4. **Synchronous preflight where needed.** Hybrid workflows such as `plan-approved` and `announce-ready-issue` run deterministic GitHub mutations inline when they do not need an agent.
5. **Prompt construction + dispatch.** The builder registry creates a `DispatchRequest`; [`../core/dispatch.py`](../core/dispatch.py) starts an Oz cloud run and saves a `RunState` record in Vercel KV.
6. **Progress comment creation.** After the Oz run id is known, the dispatch hook creates or updates the workflow progress comment and persists `progress_comment_id` in the saved run state.
7. **202 response.** The webhook returns `202 Accepted` quickly so GitHub delivery stays green.
8. **Cron drain.** [`../api/cron.py`](../api/cron.py) polls in-flight Oz runs, refreshes session links while they run, loads artifacts on success, and invokes the workflow's result applier to mutate GitHub.

The Oz run id stored as `RunState.run_id` is the canonical progress metadata identity. `progress_comment_id` is the durable GitHub comment locator used by cron-side handlers.
