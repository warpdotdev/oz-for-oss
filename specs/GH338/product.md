# Issue #338: Remove Warp-specific hardcoded values from self-improvement scripts for OSS compatibility

## Product Spec

### Summary

Oz workflows should stop assuming a Warp-specific reviewer and a `main` default branch. Introduce a repo-visible configuration file at `.github/oz/config.yml`, resolve it by choosing the consuming repository's file when present and otherwise the bundled workflow checkout's file, and use its initial `self_improvement` settings to drive reviewer assignment and default-branch behavior for every self-improvement loop.

The committed config becomes the primary, reviewable source of truth for workflow-level settings across the repository. Environment variables remain available as high-precedence overrides for one-off runs, but the default path should be visible in source control and safe for OSS adopters who use `master`, `develop`, or any other default branch name.

### Problem

The current self-improvement entrypoints still hardcode one Warp maintainer handle as the PR reviewer, and the shared push helper still assumes both the diff-comparison base and the PR base branch are `main`. Those assumptions are fine only for one repository shape. In external repositories they cause two classes of breakage:

- self-improvement PRs request review from the wrong person
- the write-surface guard and PR creation fail or target the wrong branch when the default branch is not `main`

The current knobs also live only in Python. OSS adopters cannot inspect or review them as repository configuration, and there is no single extensible place to add future workflow-level settings for other Oz workflows.

### Goals

- Remove all Warp-specific reviewer and branch-name assumptions from the shipped self-improvement workflows.
- Introduce one committed, human-editable configuration file for workflow-level settings across Oz workflows, starting with self-improvement.
- Make the config discovery order explicit: consuming repository first, bundled fallback second, with no cross-file merging.
- Support strings, integers, lists, and nested maps so future settings can be added without inventing a new format.
- Let maintainers explicitly configure reviewer handles, explicitly configure a base branch, or rely on repository-derived automatic behavior.
- Use the resolved base branch consistently for both the write-surface diff and the PR base branch.
- Document the new configuration contract in `README.md`.

### Non-goals

- Replacing existing structured repo state such as `.github/issue-triage/config.json` or `.github/STAKEHOLDERS`.
- Moving every existing environment variable into the new config file in the same change.
- Generalizing this file into a full platform policy engine.
- Changing the self-improvement loops' write surfaces, cadence, or evidence model beyond the configuration plumbing needed for this issue.

### Figma / design references

Figma: none provided. This is a repository-configuration and workflow-behavior change.

### User experience

#### Configuration file location and format

Oz workflows read a YAML file at `.github/oz/config.yml`. This issue adds the first workflow-specific section under that path for `self_improvement`; future workflows may add their own sections in the same file.

Config file discovery order:

1. `.github/oz/config.yml` in the consuming repository workspace
2. `.github/oz/config.yml` shipped inside the checked-out `oz-for-oss` workflow code

The first discovered file is the only YAML config file used for a run. The workflow does not merge values across both locations. Environment variables remain separate runtime overrides for supported self-improvement keys.

YAML is the preferred format for this file. The main drawback is adding a parser dependency, but it is still the best fit here because:

- GitHub repository maintainers already edit YAML routinely in `.github/workflows/`
- the file should be easy to read and review in pull requests
- the schema needs to grow beyond flat string values over time

To keep YAML predictable, the supported schema is narrow and versioned. The root must contain `version: 1`, and workflow-specific settings live under named sections.

Example:

```yaml
version: 1
self_improvement:
  reviewers:
    - octocat
    - repo-maintainer
  base_branch: auto
```

The initial self-improvement keys are:

- `self_improvement.reviewers`: optional list of GitHub handles written without the `@` prefix
- `self_improvement.base_branch`: optional string branch name, or `auto`

The file format must tolerate future sections and future scalar/list values without requiring a different config mechanism.

#### Reviewer behavior

Self-improvement PR reviewer selection works as follows:

1. If `SELF_IMPROVEMENT_REVIEWERS` is set, use its comma-separated handle list.
2. Else, if `self_improvement.reviewers` is present in the discovered config file, use that exact list.
3. Else, derive reviewers automatically from repository-owned metadata:
   - prefer `.github/STAKEHOLDERS` when present
   - otherwise fall back to `CODEOWNERS` / `.github/CODEOWNERS` when present
4. If no reviewers can be derived, open the PR without a reviewer request rather than tagging a baked-in fallback account.

Behavior details:

- A present but empty list (`reviewers: []`) means "do not request reviewers automatically".
- Explicit reviewer handles in `.github/oz/config.yml` and `SELF_IMPROVEMENT_REVIEWERS` must be provided without the `@` prefix.
- Derived reviewers should be based on the files the loop changed, not on a hardcoded global owner list.
- The bundled fallback config must never contain a Warp-specific reviewer handle.

#### Base branch behavior

The self-improvement loops use one resolved base branch for two things:

- `git diff --name-only origin/<base>...<branch>` for the write-surface guard
- `gh pr create --base <base>` when opening the PR

Resolution order:

1. `SELF_IMPROVEMENT_BASE_BRANCH` environment variable
2. `self_improvement.base_branch` in the discovered config file when it is a non-`auto` string
3. automatic default-branch detection from the checked-out repository

Automatic resolution must work for repositories whose default branch is `main`, `master`, `develop`, or any other valid branch name. If automatic detection fails entirely, the workflow should fail with an actionable error instead of silently assuming `main`.

#### Validation and failure behavior

- Invalid YAML, an unsupported `version`, or wrong types for active keys should fail the workflow with a message that points at `.github/oz/config.yml`.
- An explicitly configured branch that does not exist on `origin` should fail before any push or PR creation.
- A malformed explicit reviewer list should fail fast; an absent auto-derived reviewer list should degrade gracefully to "open PR without reviewer".
- Unknown future sections may be ignored, but active `self_improvement` keys must be validated strictly enough that typos in the current schema surface clearly.

#### Documentation

`README.md` should describe:

- the purpose of `.github/oz/config.yml` as the shared Oz workflow config path
- the lookup order between consuming repo and bundled fallback, and that discovery chooses one file rather than merging both
- the initial `self_improvement` keys
- the env-var overrides
- an example config snippet

### Success criteria

- No shipped self-improvement Python entrypoint contains a hardcoded reviewer handle.
- No shared helper assumes `main` as the comparison base or PR base when the config and repo metadata say otherwise.
- A consuming repository can commit `.github/oz/config.yml` and have its values take effect without editing Python code.
- A consuming repository with no config file still works by using the bundled fallback plus automatic reviewer/default-branch resolution.
- Repositories whose default branch is not `main` can run the self-improvement loops successfully.
- Repositories that want no automatic reviewer requests can express that explicitly with `reviewers: []`.
- `README.md` documents the new config path and behavior clearly enough for an OSS adopter to use it without reading the Python implementation.

### Validation

- Unit tests cover config lookup order, YAML parsing, schema validation, and env-var overrides.
- Unit tests cover explicit reviewers, derived reviewers, and the intentional no-reviewer case.
- Unit tests cover default-branch auto-detection and explicit base-branch override behavior.
- Integration-style tests for the shared push helper verify that the same resolved base branch is used for both diff calculation and PR creation.
- Manual verification in a non-`main` fixture repository confirms that a self-improvement PR can be created without changing Python code.
- Manual verification confirms that a repository with no config file no longer tags a Warp maintainer by default.

### Open questions

- Whether `LOOKBACK_DAYS` and other current self-improvement knobs should eventually move into `.github/oz/config.yml` as well.
- Whether the precedence between `.github/STAKEHOLDERS` and `CODEOWNERS` should itself become configurable, or whether the fixed "STAKEHOLDERS first, CODEOWNERS second" rule is sufficient.
