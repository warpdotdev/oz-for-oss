# Oz for OSS — Vercel control plane

This directory contains the Vercel-hosted control plane that replaces the GitHub Actions wiring in `.github/workflows/`. It exposes:

- `api/webhook.py` — receives GitHub webhook deliveries, verifies the `X-Hub-Signature-256`, and routes the event to a handler.
- `api/cron.py` — invoked on a 1-minute schedule, drains in-flight cloud runs and applies the result back to GitHub.
- `lib/` — shared helpers: signature verification, routing table, trust evaluation, dispatch, in-flight state, GitHub App token exchange.

## Why this exists

The legacy implementation runs every workflow inside its own GitHub Actions job. Each workflow spends 30–90 seconds on cold-start overhead before the agent does any real work, and a single repository can saturate the OSS Actions minutes quota.

The new control plane replaces that by:

1. Receiving the GitHub webhook directly (Vercel function, ~100ms).
2. Persisting in-flight run state in Vercel KV.
3. Letting Warp's hosted cloud agent execute the run.
4. Polling the run state on a 1-minute cron and applying the artifact back to GitHub.

There are no GitHub Actions jobs in the steady state — the runner cost shifts from per-event cold-starts onto a single Vercel project with a small KV store.

## Architecture

```
GitHub webhook --POST--> api/webhook.py
                          │
                          ├── verify signature
                          ├── route event -> workflow
                          └── (future) dispatch_run -> Oz API + KV save

(every minute) cron tick --GET--> api/cron.py
                                   │
                                   ├── list_in_flight_runs(KV)
                                   ├── retrieve run from Oz API
                                   └── on terminal state:
                                        ├── load_*_artifact
                                        ├── apply result to GitHub
                                        └── delete KV record
```

The webhook handler returns 202 immediately so the GitHub deliveries UI stays green even when the cron tick is busy.

## Cutover steps after merge

This PR ships the scaffolding. The operator must do the following before flipping the GitHub App webhook URL:

1. **Provision the Vercel project**
   - Create a new Vercel project pointing at `control-plane/` in this repo.
   - Use the Python runtime; `vercel.json` declares the function configuration.
2. **Set Vercel project secrets**
   - `OZ_GITHUB_WEBHOOK_SECRET` — the same shared secret configured on the GitHub App's webhook delivery.
   - `OZ_GITHUB_APP_ID` — the App's numeric ID.
   - `OZ_GITHUB_APP_PRIVATE_KEY` — the App's PEM-encoded private key.
   - `WARP_API_KEY` — the Warp API key the cloud agent uses.
   - `WARP_API_BASE_URL` — `https://app.warp.dev/api/v1` (or your environment's equivalent).
   - `WARP_ENVIRONMENT_ID` — default Oz cloud environment UID.
   - `WARP_REVIEW_TRIAGE_ENVIRONMENT_ID` — the Oz cloud environment for triage and PR-review runs. Falls back to `WARP_ENVIRONMENT_ID` when empty.
   - `CRON_SECRET` — random secret used to authenticate Vercel cron requests.
3. **Provision Vercel KV**
   - Add a KV resource to the project. The cron handler imports `vercel_kv` lazily so the resource binding name does not need to match anything in the code.
4. **Deploy and verify**
   - `vercel deploy --prod` from `control-plane/`.
   - Hit `https://<project>.vercel.app/api/webhook` with a curl probe to confirm the readiness GET returns 200.
   - Send a synthetic webhook delivery from the GitHub App's "Recent deliveries" UI; confirm the response is 202 and the body's `workflow` field matches the expected route.
5. **Update the GitHub App webhook URL**
   - In the GitHub App settings, change the webhook URL from the GitHub Actions delivery target to `https://<project>.vercel.app/api/webhook`.
   - Watch the next few deliveries land on Vercel.
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
