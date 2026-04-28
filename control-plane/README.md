# Oz for OSS — Vercel control plane

This directory contains the Vercel-hosted control plane that replaces the GitHub Actions wiring in `.github/workflows/`. It exposes:

- `api/webhook.py` — receives GitHub webhook deliveries, verifies the `X-Hub-Signature-256`, routes the event, and dispatches the cloud agent run via `dispatch_run`. The synchronous `enforce-pr-issue-state` decisions (allow / close) run inline; cloud-mode flows persist `RunState` to KV and return 202 with the resulting `run_id`.
- `api/cron.py` — invoked on a 1-minute schedule, drains in-flight cloud runs and applies the result back to GitHub through `lib/handlers.py`.
- `lib/` — shared helpers: signature verification, routing table, trust evaluation, dispatch, in-flight state, GitHub App token exchange, plus the per-workflow `builders.py` and `handlers.py` registries.
- `scripts/vercel_install.sh` — Vercel `installCommand` hook that mirrors `.github/scripts/oz_workflows/` and the four PR entrypoints into `lib/oz_workflows/` and `lib/scripts/` before the build step. The mirrored copies are git-ignored so `.github/scripts/` stays the single source of truth.

## What's live

PR-triggered workflows go through the control plane end-to-end:

- `review-pull-request` (PR opens, ready_for_review, `oz-review` label, `/oz-review` command).
- `respond-to-pr-comment` (`@oz-agent` mentions on PR conversation comments, review comments, and review bodies).
- `verify-pr-comment` (`/oz-verify` command).
- `enforce-pr-issue-state` (PR `synchronize` / `edited`). Synchronous allow/close decisions run inline in the webhook; only the `need-cloud-match` branch dispatches a cloud agent run.

The issue-triggered workflows (`triage-new-issues`, `respond-to-triaged-issue-comment`, `create-spec-from-issue`, `create-implementation-from-issue`) and the plan-approval workflows (`trigger-implementation-on-plan-approved`, `remove-stale-issue-labels-on-plan-approved`) are still routed by `lib/routing.py` but ignored at dispatch time. They keep flowing through the legacy GitHub Actions paths until a follow-up PR adds their builders / handlers.

## Why this exists

The legacy implementation runs every workflow inside its own GitHub Actions job. Each workflow spends 30–90 seconds on cold-start overhead before the agent does any real work, and a single repository can saturate the OSS Actions minutes quota.

The new control plane replaces that by:

1. Receiving the GitHub webhook directly (Vercel function, ~100ms).
2. Dispatching the cloud agent run fire-and-forget; persisting in-flight run state in Vercel KV.
3. Letting Warp's hosted cloud agent execute the run.
4. Polling the run state on a 1-minute cron and applying the artifact back to GitHub.

There are no GitHub Actions jobs in the steady state — the runner cost shifts from per-event cold-starts onto a single Vercel project with a small KV store.

## Architecture

```
GitHub webhook --POST--> api/webhook.py
                          │
                          ├── verify signature
                          ├── route event -> workflow
                          ├── (enforce only) sync allow/close path
                          ├── builder.build(payload) -> DispatchRequest
                          └── dispatch_run(...) -> Oz API + KV save

(every minute) cron tick --GET--> api/cron.py
                                   │
                                   ├── list_in_flight_runs(KV)
                                   ├── retrieve run from Oz API
                                   └── on terminal state:
                                        ├── handler.artifact_loader
                                        ├── handler.result_applier (apply_*_result)
                                        └── delete KV record
```

The webhook handler returns 202 immediately so the GitHub deliveries UI stays green even when the cron tick is busy.

## Cutover steps after merge

This PR ships the scaffolding. The operator must do the following before flipping the GitHub App webhook URL:

1. **Provision the Vercel project**
   - Create a new Vercel project pointing at `control-plane/` in this repo.
   - Use the Python runtime; `vercel.json` declares the function configuration. The project's `installCommand` points at `scripts/vercel_install.sh`, which mirrors `.github/scripts/oz_workflows/` and the PR entrypoints into `lib/` before the build step.
2. **Set Vercel project secrets**
   - `OZ_GITHUB_WEBHOOK_SECRET` — the same shared secret configured on the GitHub App's webhook delivery.
   - `OZ_GITHUB_APP_ID` — the App's numeric ID.
   - `OZ_GITHUB_APP_PRIVATE_KEY` — the App's PEM-encoded private key.
   - `WARP_API_KEY` — the Warp API key the cloud agent uses.
   - `WARP_API_BASE_URL` — `https://app.warp.dev/api/v1` (or your environment's equivalent).
   - `WARP_ENVIRONMENT_ID` — default Oz cloud environment UID.
   - `WARP_REVIEW_TRIAGE_ENVIRONMENT_ID` — the Oz cloud environment for triage and PR-review runs. Falls back to `WARP_ENVIRONMENT_ID` when empty.
   - `CRON_SECRET` — random secret used to authenticate Vercel cron requests.
   - `GITHUB_API_BASE_URL` *(optional)* — defaults to `https://api.github.com`. Override for GitHub Enterprise.
3. **Provision Vercel KV**
   - Add a KV resource to the project. The cron handler imports `upstash-redis` lazily and reads the auto-injected `KV_REST_API_URL` / `KV_REST_API_TOKEN` env vars.
4. **Deploy and verify**
   - `vercel deploy --prod` from `control-plane/`.
   - Hit `https://<project>.vercel.app/api/webhook` with a curl probe to confirm the readiness GET returns 200.
   - Send a synthetic webhook delivery from the GitHub App's "Recent deliveries" UI; confirm the response is 202, the body's `workflow` field matches the expected route, and `dispatched: true` plus a `run_id` is present for cloud-mode flows.
5. **Update the GitHub App webhook URL**
   - In the GitHub App settings, change the webhook URL from the GitHub Actions delivery target to `https://<project>.vercel.app/api/webhook`.
   - Watch the next few deliveries land on Vercel and confirm the cron tick drains them.
6. **Open the follow-up PR to delete the GitHub Actions workflows**
   - This PR keeps `.github/workflows/*` and `.github/actions/run-oz-python-script/` intact so the legacy path still works during the cutover. After the Vercel control plane is verified end-to-end, open a follow-up PR that deletes those files per plan §5a.

## Local development

The webhook plumbing is fully mockable. The recommended local loop is:

1. `python -m venv .venv && .venv/bin/pip install -r requirements.txt`
2. `vercel dev` — runs the same Python entrypoints behind a local web server.
3. Send synthetic webhook deliveries with the helper at `scripts/send_test_webhook.sh` (TBD — adopt your existing payload-fanout flow until that script lands).

## Testing

```sh
python -m pytest control-plane/tests
```

The test suite covers signature verification, routing, trust evaluation, dispatch, and the cron drain loop. The tests are deliberately stdlib-only so they can run alongside the legacy `.github/scripts/tests` suite without extra setup.
