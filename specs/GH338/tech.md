# Issue #338: Remove Warp-specific hardcoded values from self-improvement scripts for OSS compatibility

## Tech Spec

### Problem

The current self-improvement implementation still hardcodes repository-specific values in shared Python entrypoints. The reviewer handle is passed directly from the three shipped self-improvement scripts, and the shared push helper assumes both `origin/main` for diffing and `main` for PR creation. That breaks OSS consumers whose maintainers are not Warp employees and whose default branch is not `main`.

This change needs more than a string replacement. The fix should introduce one repo-visible, extensible configuration contract for Oz workflows, reuse the existing "consumer repo first, bundled workflow repo second" fallback model, and keep the self-improvement loops working when no consumer override exists.

### Relevant code

- `.github/scripts/update_pr_review.py:60` — passes `reviewer="captainsafia"` into `maybe_push_update_branch()`.
- `.github/scripts/update_triage.py:54` — passes `reviewer="captainsafia"` into `maybe_push_update_branch()`.
- `.github/scripts/update_dedupe.py:53` — passes `reviewer="captainsafia"` into `maybe_push_update_branch()`.
- `.github/scripts/oz/repo_local.py:145-234` — contains `changed_files_since_origin_main()` and `maybe_push_update_branch()`, which currently hardcode `origin/main` for the diff and default `base_branch="main"` for PR creation.
- `.github/scripts/oz/oz_client.py:125-196` — already implements the right lookup shape for skills: search the consuming repo workspace first, then the checked-out workflow code root.
- `.github/workflows/update-pr-review.yml`, `.github/workflows/update-triage.yml`, `.github/workflows/update-dedupe.yml` — check out the consuming repo and the workflow code separately, so the config resolver can mirror the same two-root search strategy.
- `.github/STAKEHOLDERS:1-10` — repo-local owner map already checked into source control and using CODEOWNERS-style syntax.
- `README.md:5-76` — current user-facing docs about reusable workflow setup; this is where the new config contract should be documented.

### Current state

The issue text predates the current narrowed self-improvement loops. In this checkout the hardcoded reviewer does not live in `update_spec.py` or `update_implementation.py`; it lives in the currently shipped loops `update_pr_review.py`, `update_triage.py`, and `update_dedupe.py`.

`repo_local.py` still bakes in `main` in two places:

- `changed_files_since_origin_main()` diffs against `origin/main`
- `maybe_push_update_branch()` defaults `base_branch="main"` for PR creation

There is no generic workflow config file today. The only committed structured repo state near this area is `.github/issue-triage/config.json` and `.github/STAKEHOLDERS`, which means reviewer and base-branch behavior can only be changed by editing Python and there is no shared path for other workflow-level settings.

The workflow layout already supports a two-root lookup model. Each self-improvement workflow checks out:

- the consuming repository as the main workspace
- the `oz-for-oss` workflow code under `WORKFLOW_CODE_PATH`

`oz_client.py` uses exactly that layout to resolve skills. This issue should reuse the same pattern for config instead of inventing a different discovery mechanism.

### Proposed changes

#### 1. Add a neutral, versioned workflow config file

Add `.github/oz/config.yml` to the repository with neutral defaults suitable for bundled fallback use. The file is a shared workflow config path for Oz workflows generally, with this issue only implementing the initial `self_improvement` section.

Initial shape:

```yaml
version: 1
self_improvement:
  base_branch: auto
```

`reviewers` is intentionally omitted from the bundled default so the default behavior is automatic owner derivation, not a repo-specific hardcoded list.

The config contract is:

- `version: int` — required, initially `1`
- `self_improvement.reviewers: list[str]` — optional bare GitHub handles (no leading `@`); present empty list disables reviewer requests
- `self_improvement.base_branch: str` — optional; `auto` means detect the repository default branch

YAML is chosen over JSON and TOML here. JSON is less friendly for maintainers editing by hand, and TOML would avoid a dependency but is less aligned with how GitHub maintainers already work in `.github/`. The implementation should use `yaml.safe_load` plus explicit schema validation so the parser remains predictable.

#### 2. Introduce shared config-resolution helpers

Add a new helper module, for example `.github/scripts/oz/workflow_config.py`, with two responsibilities:

- resolve `.github/oz/config.yml` using the same two-root strategy already used by `oz_client.py`
- parse and validate the `self_improvement` section into a typed object while leaving room for other top-level workflow sections in the same file

Proposed API:

```python
@dataclass(frozen=True)
class SelfImprovementConfig:
    reviewers: list[str] | None  # None => auto-derive, [] => intentionally disabled
    base_branch: str | None      # None or "auto" => detect from repo metadata

def resolve_repo_config_path(workspace_root: Path) -> Path | None: ...
def load_self_improvement_config(workspace_root: Path) -> SelfImprovementConfig: ...
```

Implementation notes:

- Extract the workflow-code-root logic from `.github/scripts/oz/oz_client.py:125-143` into a shared public helper so skills and config use the same path resolution rules.
- Search order should be:
  1. consuming repo workspace `.github/oz/config.yml`
  2. workflow code root `.github/oz/config.yml`
- Stop after the first existing file. Do not merge keys from both config locations; the discovered file is the only YAML config source for that run.
- If neither file exists, raise loudly. The bundled fallback should ship with the repository, so missing both is a packaging error rather than a user-state case.
- Environment overrides apply after selecting and loading that single file:
  - `SELF_IMPROVEMENT_REVIEWERS` overrides `reviewers` and uses the same bare-handle format as the YAML file
  - `SELF_IMPROVEMENT_BASE_BRANCH` overrides `base_branch`
- Invalid YAML, wrong `version`, or invalid active-key types should raise a `RuntimeError` with the resolved file path in the message.

#### 3. Resolve reviewers from config or repo-owned metadata

Replace the single `reviewer: str | None` contract in `maybe_push_update_branch()` with a config-driven reviewer list.

Proposed behavior:

```python
def resolve_self_improvement_reviewers(
    repo_root: Path,
    changed_files: list[str],
    config: SelfImprovementConfig,
) -> list[str]:
    ...
```

Resolution rules:

1. If config/env provided an explicit reviewers list, use it as-is.
2. If the explicit list is empty, return `[]` and omit `--reviewer`.
3. Otherwise, derive reviewers from checked-in ownership files:
   - prefer `.github/STAKEHOLDERS`
   - else try `.github/CODEOWNERS` and `CODEOWNERS`
4. Match against the changed files already computed for the write-surface guard.
5. Deduplicate handles while preserving file-order discovery.
6. If no reviewers can be derived, return `[]` and allow the PR to open without reviewer assignment.

Scope intentionally stays narrow:

- support the same simple, common CODEOWNERS-style glob subset already used by `.github/STAKEHOLDERS`
- do not attempt a full GitHub-codeowners reimplementation in this bug fix

#### 4. Resolve the base branch once and reuse it everywhere

Replace `changed_files_since_origin_main()` with a branch-agnostic helper:

```python
def changed_files_since_base_branch(
    repo_root: Path,
    branch: str,
    base_branch: str,
) -> list[str]:
    ...
```

Add a shared resolver:

```python
def resolve_self_improvement_base_branch(
    repo_root: Path,
    config: SelfImprovementConfig,
) -> str:
    ...
```

Resolution order:

1. explicit env/config branch when it is not `auto`
2. `git symbolic-ref --short refs/remotes/origin/HEAD`
3. `git remote show origin` parsed for `HEAD branch: ...`
4. fail with actionable error

The resolved branch must be used for both:

- `git diff --name-only origin/<base>...<branch>`
- `gh pr create --base <base>`

This removes both `origin/main` and `base_branch="main"` assumptions in one place.

#### 5. Update `maybe_push_update_branch()` to own config loading

Keep the self-improvement entrypoints simple by moving the new config logic into `.github/scripts/oz/repo_local.py`.

`maybe_push_update_branch()` should:

1. early-return if the branch does not exist
2. load `SelfImprovementConfig`
3. resolve the base branch
4. compute changed files against that base branch
5. run `assert_write_surface()`
6. resolve reviewers from config / ownership files
7. push the branch
8. create a PR if missing, passing `--base <resolved_branch>`
9. pass `--reviewer handle1,handle2` only when the reviewer list is non-empty

This lets the three loop entrypoints stop knowing anything about reviewer or branch configuration beyond their write surfaces and PR copy.

#### 6. Remove hardcoded reviewer arguments from the self-improvement entrypoints

Update:

- `.github/scripts/update_pr_review.py`
- `.github/scripts/update_triage.py`
- `.github/scripts/update_dedupe.py`

Each file should stop passing `reviewer="captainsafia"` to `maybe_push_update_branch()`. The call sites can remain otherwise unchanged unless the helper signature needs minor cleanup.

This issue should not add loop-specific fallback reviewers back into those scripts in another form. All reviewer selection must flow through config or derived repository ownership.

#### 7. Document the new config in `README.md`

Extend `README.md` with a short "Workflow config" section near the existing setup instructions:

- introduce `.github/oz/config.yml`
- explain the consuming-repo-first, bundled-fallback-second lookup order
- show an example with `reviewers` and `base_branch`
- mention `SELF_IMPROVEMENT_REVIEWERS` and `SELF_IMPROVEMENT_BASE_BRANCH` as override knobs
- call out that the bundled default is intentionally neutral and does not ship a Warp-specific reviewer

#### 8. Add parser dependency and tests

Add `PyYAML` to `.github/scripts/requirements.txt`.

Testing changes:

- new `.github/scripts/tests/test_workflow_config.py`
  - consuming repo config wins over bundled fallback
  - bundled fallback is used when consumer config is absent
  - env vars override file values
  - unsupported config `version` fails
  - missing `reviewers` means auto, empty list means disabled
- extend `.github/scripts/tests/test_repo_local.py`
  - `changed_files_since_base_branch()` uses the provided base branch
  - `maybe_push_update_branch()` uses the resolved base branch for both `git diff` and `gh pr create`
  - reviewer flags are omitted when reviewer resolution returns an empty list
  - reviewer flags include the configured/derived list when present
- lightweight tests for each update script to ensure the helper is invoked without a hardcoded reviewer argument

### End-to-end flow

For a consuming repository with explicit config:

1. `update-pr-review.yml` checks out the consuming repo and the workflow code.
2. `update_pr_review.py` runs Oz and leaves a local commit on `oz-agent/update-pr-review`.
3. `maybe_push_update_branch()` discovers `.github/oz/config.yml` in the consuming repo workspace and uses it as the only YAML config input for the run.
4. The helper resolves `reviewers` and `base_branch` from config/env.
5. The helper diffs against `origin/<resolved_base>`, validates the write surface, pushes the branch, and creates the PR with `--base <resolved_base>` and `--reviewer <configured reviewers>`.

For a consuming repository with no config file:

1. The helper does not find `.github/oz/config.yml` in the workspace.
2. It falls back to the bundled `.github/oz/config.yml` in the checked-out workflow code and uses that file alone.
3. `base_branch` resolves to `auto`, so the helper detects the actual default branch from git metadata.
4. `reviewers` remains auto, so the helper derives reviewer handles from `.github/STAKEHOLDERS` or CODEOWNERS when present.
5. If no owners are found, the PR opens without a reviewer instead of tagging a hardcoded Warp maintainer.

### Risks and mitigations

- Adding YAML introduces a parser dependency. Mitigation: use `yaml.safe_load`, validate a narrow schema, and keep the config small.
- Auto-derived reviewer matching may produce no owners or slightly broader owner sets than intended. Mitigation: explicit `reviewers` always wins, and `reviewers: []` disables auto-assignment cleanly.
- Some clones may not have `origin/HEAD` configured. Mitigation: use a two-step auto-detection path and fail loudly only after both git-based checks fail.
- A repo-specific reviewer handle could accidentally be reintroduced into the bundled fallback config. Mitigation: keep the fallback file neutral and add a regression test that the bundled config has no explicit reviewers.
- Unknown future keys could hide typos. Mitigation: validate the active `self_improvement` keys strictly while allowing unrelated future sections to exist.

### Testing and validation

- Run `env PYTHONPATH=.github/scripts python -m unittest discover -s .github/scripts/tests`.
- Add unit coverage for config lookup, schema validation, reviewer resolution, and base-branch resolution.
- Add command-construction coverage around `maybe_push_update_branch()` so the test suite proves the same base branch is used for diffing and PR creation.
- Manually verify in a fixture repo whose default branch is `develop` that the write-surface guard and PR creation both target `develop`.
- Manually verify in a fixture repo with no config file and no ownership files that the workflow opens a PR without reviewer assignment rather than tagging a Warp maintainer.

### Follow-ups

- Consider moving `LOOKBACK_DAYS` into `.github/oz/config.yml` once this config path exists and has at least one other consumer.
- Consider reusing the same config file for other repo-visible workflow knobs after there is a second concrete need, rather than expanding it preemptively in this change.
