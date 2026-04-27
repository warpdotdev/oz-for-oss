# oz-for-oss

Oz for OSS contains a set of workflows to help manage the overhead of maintaining open-source project. It consists of workflows that trigger Oz agents to triage issues, generate product and tech specs, create implementation PRs, and reviews pull requests.

The automation is organized as GitHub Actions workflows under `.github/workflows/` that invoke Python entrypoints in `.github/scripts/` (with shared helpers in `.github/scripts/oz_workflows/`), backed by triage label definitions in `.github/issue-triage/`, a CODEOWNERS-style stakeholder map in `.github/STAKEHOLDERS`, and committed spec artifacts under `specs/GH{number}/product.md` and `specs/GH{number}/tech.md`. Together these cover issue triage, product and tech spec creation, issue implementation scaffolding, PR issue-state enforcement, PR review orchestration, and unready-assignment guidance for Oz.

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

Optionally, set the following **repository variables** (not secrets) to customize agent behavior:

| Variable | Description |
|---|---|
| `WARP_AGENT_MODEL` | Override the default Oz model (e.g. a specific model identifier). |
| `WARP_AGENT_MCP` | MCP configuration for the agent, if any. |
| `WARP_ENVIRONMENT_ID` | Cloud environment UID for Oz agent runs. |

### 3. Add local adapter workflows

The reusable workflows in this repository are invoked via `workflow_call`. Your target repository needs thin local adapter workflows that map GitHub events to the reusable workflows.

Use the `*-local.yml` files in this repository as reference adapters. Copy them into `.github/workflows/` in your target repository and change each `uses:` ref from `./.github/workflows/<workflow>.yml` to `warpdotdev/oz-for-oss/.github/workflows/<workflow>.yml@main`.

- **Issue triage** — [`triage-new-issues-local.yml`](.github/workflows/triage-new-issues-local.yml)
- **Spec creation** — [`create-spec-from-issue-local.yml`](.github/workflows/create-spec-from-issue-local.yml)
- **Implementation** — [`create-implementation-from-issue-local.yml`](.github/workflows/create-implementation-from-issue-local.yml)
- **PR review and enforcement** — [`pr-hooks.yml`](.github/workflows/pr-hooks.yml) (orchestrates `enforce-pr-issue-state.yml`, `run-tests.yml`, and `review-pull-request.yml` together)
- **Respond to PR comments** — [`respond-to-pr-comment-local.yml`](.github/workflows/respond-to-pr-comment-local.yml)
- **Respond to triaged-issue comments** — [`respond-to-triaged-issue-comment-local.yml`](.github/workflows/respond-to-triaged-issue-comment-local.yml)
- **Unready-assignment guard** — [`comment-on-unready-assigned-issue-local.yml`](.github/workflows/comment-on-unready-assigned-issue-local.yml)
- **Review skill updates** — [`update-pr-review-local.yml`](.github/workflows/update-pr-review-local.yml) (scheduled weekly)
- **On-demand PR verification (`/oz-verify`)** — [`oz-verify-local.yml`](.github/workflows/oz-verify-local.yml) (invokes [`oz-verify.yml`](.github/workflows/oz-verify.yml))

Each adapter is deliberately thin — it defines the GitHub event triggers and conditions, then delegates to the reusable workflow.

### 4. `/oz-verify` slash command

The [`oz-verify.yml`](.github/workflows/oz-verify.yml) reusable workflow lets collaborators and verified members of the repository owner's organization run end-to-end verification on a pull request on demand. Adopt it in a target repository by copying [`oz-verify-local.yml`](.github/workflows/oz-verify-local.yml) into `.github/workflows/` and changing the `uses:` ref to `warpdotdev/oz-for-oss/.github/workflows/oz-verify.yml@main`.

Usage:

- Post `/oz-verify` as a new comment on a pull request to verify it with every verification skill in the repository (see *Authoring verification skills* below for the frontmatter tag convention).
- Post `/oz-verify <skill-name>` (e.g. `/oz-verify verify-login-flow`) to run a single verification skill.
- Only comments from collaborators, repository members/owners, or confirmed members of the repository owner's organization trigger the workflow.

The orchestration loop lives in [`.github/scripts/oz_verify.py`](.github/scripts/oz_verify.py). For each discovered verification skill, the script launches an Oz agent run with that skill against the PR HEAD, polls for a `verification_report.md` artifact, and uses the Oz SDK to resolve signed download URLs for any screenshot or video artifacts the skill uploaded. It rewrites any standard Markdown image or link URL in the report that points at an uploaded artifact's filename (e.g. `![login success](login-success.png)`) to that artifact's signed download URL, then posts a single consolidated PR comment that includes every skill's report plus a link to the workflow run.

#### Required secrets and permissions

- `WARP_API_KEY` must be set in repository or organization secrets.
- `OZ_MGMT_GHA_APP_ID` and `OZ_MGMT_GHA_PRIVATE_KEY` must be set for the shared Oz GitHub App, just like the other reusable workflows in this repo.
- The calling adapter grants `issues: write` and `pull-requests: write` so the workflow can react to the trigger comment and post the consolidated verification comment. `contents: read` is sufficient — the `/oz-verify` flow never pushes to the PR branch or to any side branch; media artifacts are served directly from signed download URLs resolved through the Oz SDK.

#### Authoring verification skills

A verification skill is any skill that follows this convention:

- It lives at `.agents/skills/<name>/SKILL.md` and exposes standard skill frontmatter (`name`, `description`).
- Its frontmatter includes a top-level [`metadata`](https://agentskills.io/specification#metadata-field) mapping whose `verification` key is set to `"true"`. This tag is the single source of truth for whether `/oz-verify` runs the skill — directory name and description wording do not matter.
- It reads the checked-out PR HEAD and writes a concise `verification_report.md` with pass/fail status, then uploads that report (and any screenshot/video evidence) as Oz artifacts via `oz-dev artifact upload <path>`.
- It references screenshot/video evidence from the report using standard Markdown image or link syntax whose URL is the uploaded artifact's filename (e.g. `![login](login-success.png)`). The workflow rewrites each such URL with a signed URL sourced from the Oz run's artifacts, so skills must not construct image or video URLs themselves.

Verification skills should be read-only with respect to tracked files: they can run the repository's tests, scripts, or tooling, but must not commit, push, or modify tracked files. See [`.agents/skills/verify-pr/SKILL.md`](.agents/skills/verify-pr/SKILL.md) for the full contract, including the report structure, the artifact-upload convention, and the Markdown syntax used for inline embeds.

### 5. Bootstrap triage configuration (optional)

If you want the triage agent to apply area and status labels, run the `bootstrap-issue-config` skill on your target repository. The skill fetches existing labels and classifies them into area, feature, and status categories; analyzes recent issues and issue templates to discover additional labels; generates or updates `.github/issue-triage/config.json` with label definitions (colors and descriptions); generates or updates `.github/STAKEHOLDERS` by inspecting CODEOWNERS, recent git contributors, and existing stakeholder information; and creates any missing labels on the repository via the GitHub API.

The skill is idempotent — re-running it merges new discoveries with existing configuration rather than overwriting it. The `config.json` file contains **only** label definitions; stakeholder ownership is managed separately in `.github/STAKEHOLDERS`, which uses the same glob-based syntax as GitHub CODEOWNERS files.

## Local development

### Setup

```sh
python3 -m venv .venv
source .venv/bin/activate.fish
python -m pip install --upgrade pip
python -m pip install -r .github/scripts/requirements.txt
```

### Run tests

```sh
env PYTHONPATH=.github/scripts python -m unittest discover -s .github/scripts/tests
```

### Run workflow entrypoints locally

The scripts under `.github/scripts/` are designed to run inside GitHub Actions, so they expect the same event payload and environment variables that the workflows provide. For local debugging, point `PYTHONPATH` at `.github/scripts/`, provide the relevant GitHub Actions environment variables, and execute the entrypoint you want to inspect.

Common entrypoints include:

- `.github/scripts/triage_new_issues.py`
- `.github/scripts/create_spec_from_issue.py`
- `.github/scripts/create_implementation_from_issue.py`
- `.github/scripts/enforce_pr_issue_state.py`
- `.github/scripts/review_pr.py`
- `.github/scripts/oz_verify.py`
