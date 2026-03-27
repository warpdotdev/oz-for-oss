---
name: bootstrap-issue-config
description: Bootstrap the issue triage configuration for a repository by analyzing existing issues, labels, and contributors to generate `.github/issue-triage/config.json` and `.github/STAKEHOLDERS`. Use when setting up triage automation on a new or existing repository for the first time.
---

# Bootstrap issue triage configuration

Analyze the target repository and generate (or update) the issue triage configuration files used by the `triage-new-issues` workflow.

## Outputs

This skill produces two files:

1. **`.github/issue-triage/config.json`** — label definitions used during triage.
2. **`.github/STAKEHOLDERS`** — CODEOWNERS-style ownership mappings from path patterns to GitHub usernames.

## Workflow

### 1. Discover existing labels

- Use `gh label list --repo <owner>/<repo> --limit 200 --json name,color,description` to fetch all labels currently defined on the repository.
- Classify each label into one of three categories:
  - **area** labels — identify a component or subsystem (e.g. `area:api`, `area:docs`).
  - **feature** labels — identify a capability or request type (e.g. `enhancement`, `bug`, `documentation`).
  - **status** labels — identify workflow state (e.g. `triaged`, `needs-info`, `wontfix`).
- If the repository has very few or no labels, seed the config with sensible defaults:
  - `triaged` (status), `bug` (feature), `enhancement` (feature), `documentation` (feature), `needs-info` (status), `duplicate` (status)
  - `repro:high`, `repro:medium`, `repro:low`, `repro:unknown` (status)

### 2. Analyze recent issues

- Use `gh issue list --repo <owner>/<repo> --state all --limit 100 --json number,title,labels,createdAt` to fetch recent issues.
- If issues use labels that are not yet captured, add them to the appropriate category.
- Look at `.github/ISSUE_TEMPLATE/` for template files — template names and labels referenced in templates can inform label discovery.

### 3. Generate or update `config.json`

- Read any existing `.github/issue-triage/config.json`.
- Merge newly discovered labels into the existing `labels` object. Do **not** remove labels that already exist in the config — only add or update.
- The config must contain **only** the `labels` key. Do **not** include `stakeholders` or `default_experts`.
- Each label entry should have `color` (6-character hex without `#`) and `description` (one-sentence summary).
- Write the result to `.github/issue-triage/config.json`.
- Validate with `jq . .github/issue-triage/config.json`.

### 4. Generate or update `.github/STAKEHOLDERS`

- Inspect `CODEOWNERS` if it exists for initial ownership hints.
- Use `git log --format='%aN <%aE>' --since='6 months ago' -- <path>` and `gh api` to identify recent contributors to major directories.
- Read any existing `.github/STAKEHOLDERS` file and merge new entries rather than overwriting.
- Write the file using CODEOWNERS conventions:
  ```
  # Syntax follows CODEOWNERS conventions: later rules take precedence.
  # NOTE: This file is advisory only — GitHub does not enforce it.

  # --- Section comment ---
  /path/pattern/ @owner1 @owner2
  ```
- Each line maps a path glob to one or more `@username` owners.

### 5. Create missing labels

- For every label in the final `config.json` that does not already exist on the repository, create it:
  ```
  gh label create "<name>" --color "<color>" --description "<description>" --repo <owner>/<repo>
  ```
- Skip labels that already exist (the `gh label create` command will error on duplicates — ignore those errors).

### 6. Validate and summarize

- Re-validate `config.json` with `jq`.
- Print a short summary of:
  - How many labels were discovered vs. newly created.
  - How many stakeholder entries were written.
  - Any warnings (e.g. no issues found, no CODEOWNERS file).

## Idempotency

This skill is designed to be run multiple times safely. Re-running will:
- Merge new labels into the existing config without removing old ones.
- Merge new stakeholder entries without duplicating existing lines.
- Skip label creation for labels that already exist on the repository.

## Assumptions

- The `gh` CLI is authenticated and has access to the target repository.
- The skill is run from the repository root.
- The target repository is the current working directory unless the prompt specifies otherwise.
