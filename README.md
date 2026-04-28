# oz-for-oss

Oz for OSS contains a set of workflows to help manage the overhead of maintaining an open-source project. It consists of workflows that trigger Oz agents to triage issues, generate product and tech specs, create implementation PRs, and review pull requests.

There are two delivery surfaces:

- **GitHub Actions workflows** under `.github/workflows/` invoke Python entrypoints in `.github/scripts/` (with shared helpers in `.github/scripts/oz_workflows/`).
- **Vercel control plane** under [`control-plane/`](control-plane/) hosts a webhook receiver and a 1-minute cron poller that dispatches Warp-hosted cloud agent runs and applies their results back to GitHub.

Both paths share the same Python helpers, agent prompts, and artifact contracts. The Vercel control plane stores in-flight cloud agent runs in Vercel KV and drains them on the cron tick. Triage label definitions live in `.github/issue-triage/`, the CODEOWNERS-style stakeholder map lives in `.github/STAKEHOLDERS`, and committed spec artifacts live under `specs/GH{number}/product.md` and `specs/GH{number}/tech.md`. Together these cover issue triage, product and tech spec creation, issue implementation scaffolding, PR issue-state enforcement, PR review orchestration, and unready-assignment guidance for Oz.

## Have an open-source project?

Actively maintained open-source projects can apply for the [Oz Open Source Partnership](https://docs.warp.dev/support-and-community/community/open-source-partnership) to receive free Oz credits for using these workflows. Accepted projects can use Oz agents for tasks like issue triage, pull request review, documentation, and implementation support across their repositories.

To apply, [fill out the application form](https://tally.so/r/LZWxqG) and we'll be in touch.

## How to use these workflows in your own repo

To use the `oz-for-oss` reusable workflows in another repository, you need a GitHub App installation, a set of GitHub Actions secrets and variables, and local adapter workflows that call the reusable layer.

### 1. Create and install a GitHub App

The workflows authenticate through a GitHub App rather than a personal access token. Create an app under your organization (or personal account) with these permissions:

**Repository permissions**

- **Contents** — Read & Write (checkout code, push branches)
- **Issues** — Read & Write (apply labels, post comments, manage assignees)
- **Pull requests** — Read & Write (open PRs, post reviews)

**Organization permissions**

- None required.

After creating the app, install it on every repository that will use the workflows. Note the **App ID** and generate a **private key** — both are needed in the next step.

### 2. Configure GitHub Actions secrets and variables

Add the following **secrets** to each target repository (or at the organization level):

| Secret | Description |
|---|---|
| `OZ_MGMT_GHA_APP_ID` | The numeric App ID of the GitHub App created above. |
| `OZ_MGMT_GHA_PRIVATE_KEY` | The PEM-encoded private key for that App. |
| `WARP_API_KEY` | Your Warp API key, used to invoke Oz agents. |

Set the following **repository variable** (not a secret) for reusable workflows
that invoke Oz cloud agents directly:

| Variable | Description |
|---|---|
| `WARP_ENVIRONMENT_ID` | **Required** for workflows that call the Oz API to run cloud agents (for example, spec creation, implementation, PR review, and PR/issue comment response workflows). Set this to the Oz cloud environment UID the agent should run in. You can find the UID with `oz environment list` or on the environment details page in the Oz web app. |
 
Optionally, set the following additional **repository variables** (not secrets)
to customize agent behavior:

| Variable | Description |
|---|---|
| `WARP_AGENT_MODEL` | Override the default Oz model (e.g. a specific model identifier). |
| `WARP_REVIEW_TRIAGE_ENVIRONMENT_ID` | Optional: route the triage and PR-review workflows (`triage-new-issues`, `respond-to-triaged-issue-comment`, `review-pull-request`) to a dedicated cloud environment, separate from the default `WARP_ENVIRONMENT_ID` used by spec/implementation/comment-response workflows. When unset, those workflows fall back to `WARP_ENVIRONMENT_ID` and behave identically to the legacy single-environment setup. Useful for giving the lighter-weight triage/review pool tighter resource limits. |

### 3. Add local adapter workflows

The reusable workflows in this repository are invoked via `workflow_call`. Your target repository needs thin local adapter workflows that map GitHub events to the reusable workflows.

Use the `*-local.yml` files in this repository as reference adapters. Copy them into `.github/workflows/` in your target repository and change each `uses:` ref from `./.github/workflows/<workflow>.yml` to `warpdotdev/oz-for-oss/.github/workflows/<workflow>.yml@main`.
The reusable workflows delegate their shared helper logic through composite actions in `warpdotdev/oz-for-oss/.github/actions/` rather than doing a second checkout of this repository into the caller workspace.

- **Issue triage** — [`triage-new-issues-local.yml`](.github/workflows/triage-new-issues-local.yml)
- **Spec creation** — [`create-spec-from-issue-local.yml`](.github/workflows/create-spec-from-issue-local.yml)
- **Implementation** — [`create-implementation-from-issue-local.yml`](.github/workflows/create-implementation-from-issue-local.yml)
- **PR review and enforcement** — [`pr-hooks.yml`](.github/workflows/pr-hooks.yml) (orchestrates `enforce-pr-issue-state.yml`, `run-tests.yml`, and `review-pull-request.yml` together)
- **Respond to PR comments** — [`respond-to-pr-comment-local.yml`](.github/workflows/respond-to-pr-comment-local.yml)
- **PR verification via slash command** — [`verify-pr-comment-local.yml`](.github/workflows/verify-pr-comment-local.yml) (runs when a trusted PR comment contains `/oz-verify`)
- **Respond to triaged-issue comments** — [`respond-to-triaged-issue-comment-local.yml`](.github/workflows/respond-to-triaged-issue-comment-local.yml)
- **Unready-assignment guard** — [`comment-on-unready-assigned-issue-local.yml`](.github/workflows/comment-on-unready-assigned-issue-local.yml)
- **Review skill updates** — [`update-pr-review-local.yml`](.github/workflows/update-pr-review-local.yml) (scheduled weekly)

Each adapter is deliberately thin — it defines the GitHub event triggers and conditions, then delegates to the reusable workflow.

### 4. Configure shared Oz workflow settings (optional)

Repositories can commit `.github/oz/config.yml` to make workflow-level defaults visible and reviewable in source control. Oz resolves that file from the consuming repository first; if it is absent there, the workflows fall back to the bundled `.github/oz/config.yml` shipped with `oz-for-oss`. Discovery stops at the first existing file — the two locations are not merged.

The initial supported settings live under `self_improvement`:

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

- `self_improvement.reviewers` — optional list of GitHub handles. Set `[]` to disable automatic reviewer requests.
- `self_improvement.base_branch` — optional branch name, or `auto` to detect the repository default branch from git metadata.
- `triage.prior_triage_labels` — optional list of labels that should count as evidence that Oz has already triaged an issue. Defaults to `["triaged"]`.
- `SELF_IMPROVEMENT_REVIEWERS` and `SELF_IMPROVEMENT_BASE_BRANCH` remain high-precedence overrides for one-off runs.
- Provide reviewer handles without the `@` prefix in both `.github/oz/config.yml` and `SELF_IMPROVEMENT_REVIEWERS`.

The bundled fallback config is intentionally neutral: it does not ship a Warp-specific reviewer list and defaults the base branch to `auto`.

### 5. Bootstrap triage configuration (optional)

If you want the triage agent to apply area and status labels, run the `bootstrap-issue-config` skill on your target repository. The skill fetches existing labels and classifies them into area, feature, and status categories; analyzes recent issues and issue templates to discover additional labels; generates or updates `.github/issue-triage/config.json` with label definitions (colors and descriptions); generates or updates `.github/STAKEHOLDERS` by inspecting CODEOWNERS, recent git contributors, and existing stakeholder information; and creates any missing labels on the repository via the GitHub API.

The skill is idempotent — re-running it merges new discoveries with existing configuration rather than overwriting it. The `config.json` file contains **only** label definitions; stakeholder ownership is managed separately in `.github/STAKEHOLDERS`, which uses the same glob-based syntax as GitHub CODEOWNERS files.

## Vercel control plane

[`control-plane/`](control-plane/) hosts the Vercel-deployed webhook receiver + cron poller that the GitHub App can route deliveries to instead of GitHub Actions. The webhook handler verifies the `X-Hub-Signature-256` HMAC, picks a workflow handler, and persists in-flight cloud run state in Vercel KV. The 1-minute cron tick reads the KV state, polls Oz for terminal status, and applies completed runs back to GitHub through workflow-specific result handlers.

The control plane is the recommended steady-state delivery target — the webhook handler returns 202 in ~100ms and avoids the 30–90 second cold-start tax of every GitHub Actions job. The legacy GitHub Actions workflows in `.github/workflows/` continue to work in parallel until the operator flips the GitHub App's webhook URL; once the Vercel control plane is verified end-to-end, a follow-up PR can delete the workflow YAMLs.

See [`control-plane/README.md`](control-plane/README.md) for the full deployment runbook (Vercel project setup, KV provisioning, secret variables, and the GitHub App webhook URL change).

## Local development

### Setup

```sh
python3 -m venv .venv
source .venv/bin/activate.fish
python -m pip install --upgrade pip
python -m pip install -r .github/scripts/requirements.txt
```

### Run tests

The legacy GitHub Actions Python suite:

```sh
env PYTHONPATH=.github/scripts python -m unittest discover -s .github/scripts/tests
```

The Vercel control-plane suite:

```sh
python -m pytest control-plane/tests
```

### Run workflow entrypoints locally

The scripts under `.github/scripts/` are designed to run inside GitHub Actions, so they expect the same event payload and environment variables that the workflows provide. For local debugging, point `PYTHONPATH` at `.github/scripts/`, provide the relevant GitHub Actions environment variables, and execute the entrypoint you want to inspect.

Common entrypoints include:

- `.github/scripts/triage_new_issues.py`
- `.github/scripts/create_spec_from_issue.py`
- `.github/scripts/create_implementation_from_issue.py`
- `.github/scripts/enforce_pr_issue_state.py`
- `.github/scripts/review_pr.py`
