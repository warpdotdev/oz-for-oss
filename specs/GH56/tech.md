# Issue #56: Support agent skill for bootstrapping issues config metadata

## Problem
The triage-issue workflow relies on `.github/issue-triage/config.json` for label definitions, stakeholder routing, and default experts. This config is hand-authored today, and its structure conflates label taxonomy with ownership information. The issue asks for three changes:

1. A new skill that bootstraps `config.json` by analyzing a repo's existing issues to discover area, feature, and status labels — plus a workflow component that creates missing labels via the GitHub API.
2. Move stakeholder ownership out of `config.json` and into a CODEOWNERS-style `.github/STAKEHOLDERS` file, matching the pattern used in `warpdotdev/warp-server`.
3. Remove the `default_experts` concept entirely.

The triggering comment also asks that repo docs be updated to explain how to use the new bootstrapping tool.

## Current state
- `config.json` (`/.github/issue-triage/config.json`) has three top-level keys: `labels` (dict of label specs), `stakeholders` (list of ownership entries with `path_prefixes`, `experts`, `default_labels`), and `default_experts` (list of fallback GitHub logins).
- `load_triage_config()` in `src/oz/triage.py` validates that `labels` is a dict and `stakeholders` is a list.
- `src/triage_new_issues.py` loads the config, passes the full JSON into the agent prompt, and uses `configured_labels` to ensure labels exist in the repo (via `ensure_label_exists()`).
- The `triage-issue` skill references stakeholder config and default experts in steps 5–6.
- No skill currently exists for bootstrapping or syncing the config.
- `warpdotdev/warp-server` uses a `.github/STAKEHOLDERS` file with CODEOWNERS-style glob syntax and section comments, paired with a `sync-stakeholders` skill.
- Existing tests in `src/tests/test_triage.py` cover `load_triage_config` and expect `labels`+`stakeholders` keys.

## Proposed changes

### 1. New `bootstrap-issue-config` skill
Create `.agents/skills/bootstrap-issue-config/SKILL.md` with instructions for an agent to:
- Fetch recent open and closed issues from the target repository using `gh` CLI or the GitHub API.
- Analyze existing issue labels and classify them into categories: **area** labels (component/subsystem), **feature** labels (capability/request type), and **status** labels (workflow state like `triaged`, `needs-info`).
- Analyze existing issue templates in `.github/ISSUE_TEMPLATE/` to inform label discovery.
- Generate or update `.github/issue-triage/config.json` with a `labels` object containing discovered labels (with sensible colors and descriptions).
- Generate or update `.github/STAKEHOLDERS` by inspecting `CODEOWNERS`, recent git contributors, and any existing stakeholder information.
- The skill should be idempotent — re-running it should merge new discoveries with existing config rather than overwriting.
- After generating the config, validate the JSON with `jq`.
- The skill should NOT include `stakeholders` or `default_experts` in `config.json` — those concepts move to `STAKEHOLDERS`.

### 2. Move stakeholders to `.github/STAKEHOLDERS`
Create a `.github/STAKEHOLDERS` file using the same CODEOWNERS-style format as warp-server:
```
# Syntax follows CODEOWNERS conventions: later rules take precedence.
# NOTE: This file is advisory only — GitHub does not enforce it.

# --- Workflow and automation ---
/src/ @captainsafia
/.github/workflows/ @captainsafia
/.github/issue-triage/ @captainsafia

# --- Documentation and plans ---
/README.md @captainsafia
/CONTRIBUTING.md @captainsafia
/plans/ @captainsafia
```

#### Python changes to load STAKEHOLDERS
Add a `load_stakeholders(path: Path) -> list[dict]` function in `src/oz/triage.py` that parses the CODEOWNERS-style file into a list of structured entries (path pattern → list of owners). Each non-comment, non-blank line becomes an entry with `pattern` and `owners` fields.

#### Update `load_triage_config`
- Remove the requirement that `stakeholders` exists in `config.json`.
- Remove the requirement or handling of `default_experts` from `config.json`.
- The function should only validate that `labels` is a dict.

#### Update `config.json`
Remove `stakeholders` and `default_experts` keys from `.github/issue-triage/config.json`, leaving only `labels`.

#### Update `triage_new_issues.py`
- After loading triage config, also load `.github/STAKEHOLDERS` using the new parser.
- Pass stakeholder information (formatted from the STAKEHOLDERS file) into the agent prompt instead of embedding it in the triage config JSON.
- Remove any references to `default_experts` from the prompt construction.

### 3. Remove default experts
- Delete the `default_experts` key from `config.json`.
- Remove references to "default experts" from the `triage-issue` skill (step 5 currently says "using the configured default experts only when there is no stronger match").
- The triage skill should fall back to recent git contributors when no stakeholder match is found, with no further fallback.

### 4. Update the `triage-issue` skill
Update `.agents/skills/triage-issue/SKILL.md`:
- Step 5: Replace stakeholder config references with STAKEHOLDERS file references; remove default-experts fallback.
- Inputs section: Note that stakeholder information now comes from a STAKEHOLDERS file rather than being embedded in the triage config JSON.

### 5. Update documentation
Update `README.md`:
- Add a section describing the `bootstrap-issue-config` skill and how to use it to set up a new repository's triage configuration.
- Update the "Primary artifacts" section to mention `.github/STAKEHOLDERS`.
- Note that `.github/issue-triage/config.json` now contains only label definitions.

### 6. Update tests
- Update `test_triage.py` `LoadTriageConfigTest` to reflect that `stakeholders` is no longer required in config.json.
- Add tests for the new `load_stakeholders()` parser.

## File change summary
New files:
- `.agents/skills/bootstrap-issue-config/SKILL.md`
- `.github/STAKEHOLDERS`

Modified files:
- `.github/issue-triage/config.json` — remove `stakeholders` and `default_experts`
- `src/oz/triage.py` — update `load_triage_config`, add `load_stakeholders`
- `src/triage_new_issues.py` — load STAKEHOLDERS, update prompt, remove default_experts
- `.agents/skills/triage-issue/SKILL.md` — update stakeholder/expert references
- `README.md` — document new bootstrapping skill and STAKEHOLDERS file
- `src/tests/test_triage.py` — update config test, add stakeholders parser tests

## Risks and open questions
- **STAKEHOLDERS parser complexity**: The CODEOWNERS format supports glob patterns and team references (`@org/team`). For the initial implementation, supporting simple path prefixes and `@username` references is sufficient. Team alias support can be added later.
- **Backward compatibility**: Repos that already have `stakeholders` in their config.json will break after this change unless they also add a STAKEHOLDERS file. The bootstrap skill should handle migration, but we should note this in docs.
- **Bootstrap skill scope**: The skill analyzes existing issues to discover labels. Repos with few or no issues will produce a minimal config. The skill should have sensible defaults for common label patterns (bug, enhancement, documentation, needs-info, repro levels).
- **Label creation in workflow**: The existing `ensure_label_exists()` function in `triage_new_issues.py` already creates labels at triage time. The issue asks for a workflow component that creates labels from config — this can be a step in the bootstrap skill rather than a separate workflow, since the skill already has access to `gh` CLI.
