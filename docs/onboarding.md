# Onboarding

Because the bot has two delivery surfaces, onboarding into your own repo has two parts: a GitHub App and a Vercel project, plus repo-level secrets and adapter workflows.

## 1. Set up the GitHub App

The webhook handler authenticates as a GitHub App and so do the issue-triggered Actions. Create the App (organization-owned or user-owned), grant it these permissions, and install it on every repository that should receive the bot:

**Repository permissions**

- **Contents** — Read & Write (checkout code, push branches)
- **Issues** — Read & Write (apply labels, post comments, manage assignees)
- **Pull requests** — Read & Write (open PRs, post reviews)

**Webhook events**

- `issues`, `issue_comment`, `pull_request`, `pull_request_review_comment`

Note the **App ID** and a generated **private key** — both are needed for the GitHub Actions secrets and the Vercel project secrets.

## 2. Provision the Vercel webhook control plane

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

## 3. Configure GitHub Actions secrets and variables

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

## 4. Add local adapter workflows

For each issue-triggered or plan-approval flow you want, copy the matching `*-local.yml` file into your repository's `.github/workflows/` and update the `uses:` reference from `./.github/workflows/<workflow>.yml` to `warpdotdev/oz-for-oss/.github/workflows/<workflow>.yml@main`. PR-triggered flows (`review-pull-request`, `enforce-pr-issue-state`, `respond-to-pr-comment`, `verify-pr-comment`) do **not** need adapter YAMLs anymore — the webhook control plane handles them as soon as the GitHub App's webhook URL is wired to your Vercel project.

## 5. Configure shared Oz workflow settings (optional)

Repositories can commit `.github/oz/config.yml` to make workflow-level defaults visible and reviewable in source control. Oz resolves that file from the consuming repository first and falls back to the bundled [`../.github/oz/config.yml`](../.github/oz/config.yml) when absent. Discovery stops at the first existing file — the two locations are not merged. The settings live under `self_improvement` and `triage`:

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

## 6. Bootstrap triage configuration (optional)

Run the [`bootstrap-issue-config`](../.agents/skills/bootstrap-issue-config/SKILL.md) skill against your repository to seed `.github/issue-triage/config.json` and `.github/STAKEHOLDERS` with sensible defaults derived from your existing labels and CODEOWNERS.
