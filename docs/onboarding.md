# Onboarding

Onboarding a repository to `oz-for-oss` requires a GitHub App and a Vercel project. Agent-backed behavior is delivered entirely through the Vercel webhook control plane; consuming repositories do not need reusable GitHub Actions workflow adapters.

## 1. Set up the GitHub App

Create the App (organization-owned or user-owned), grant it these permissions, and install it on every repository that should receive the bot:

**Repository permissions**

- **Contents** — Read & Write (checkout code, push branches)
- **Issues** — Read & Write (apply labels, post comments, manage assignees)
- **Pull requests** — Read & Write (open PRs, post reviews)

**Webhook events**

- `issues`, `issue_comment`, `pull_request`, `pull_request_review`, `pull_request_review_comment`

Note the **App ID** and a generated **private key**. The Vercel webhook uses them to mint installation tokens for repository operations.

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
| `CRON_SECRET` | Required random secret used to authenticate Vercel cron requests. Local development can opt out with `OZ_ALLOW_UNAUTHENTICATED_CRON=true`. |
| `GITHUB_API_BASE_URL` | Optional. Defaults to `https://api.github.com`. Override for GitHub Enterprise. |

Provision a Vercel KV resource on the project. Vercel injects `KV_REST_API_URL` / `KV_REST_API_TOKEN` automatically; the cron handler reads them at runtime through `upstash-redis`.

Finally, point the GitHub App's webhook URL at `https://<vercel-project>.vercel.app/api/webhook`. The webhook handler returns `202` for every delivery so the App's "Recent deliveries" UI stays green even when the cron tick is busy.

## 3. Configure shared Oz workflow settings (optional)

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

## 4. Bootstrap triage configuration (optional)

Run the [`bootstrap-issue-config`](../.agents/skills/bootstrap-issue-config/SKILL.md) skill against your repository to seed `.github/issue-triage/config.json` and `.github/STAKEHOLDERS` with sensible defaults derived from your existing labels and CODEOWNERS.
