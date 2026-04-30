# Issue #251: Pull repo-tunable state out of core agents into repo-specific skills for self-improvement loops

## Tech Spec

### Problem

The reusable agent skills and the `update-pr-review` self-improvement loop in this repo conflate a stable cross-repo contract with repo-specific preferences. We need a concrete implementation plan that:

- moves repo-specific guidance out of the core skill bodies and out of `.github/scripts/*` conditionals into a repo-local skill layer
- extends the prompt-construction layer to reference that repo-local layer as additional context at runtime, without inlining its contents into the prompt
- narrows the write surface of `update-pr-review` to only the repo-local layer
- introduces analogous `update-triage` and `update-dedupe` self-improvement loops (dedupe signals feed `update-dedupe` specifically)
- documents the pattern in `docs/platform.md` so other repos can adopt it

This spec translates the product spec at `specs/GH251/product.md` into file-level changes against the current codebase.

### Relevant code

Core skill files that currently mix contract and repo-specific guidance:

- `.agents/skills/review-pr/SKILL.md` — contains user-facing-string norms, graceful-degradation rules, debugging/observability rules that are Warp/Oz-specific rather than universal.
- `.agents/skills/review-spec/SKILL.md` — contains spec-section expectations that reflect this repo's `specs/GH<n>/` convention.
- `.agents/skills/triage-issue/SKILL.md` — contains the issue-shape taxonomy and follow-up patterns that are Warp-specific (for example the `area:keyboard-layout` guidance and the list of "environment-sensitive bugs" patterns).
- `.agents/skills/dedupe-issue/SKILL.md` — the core dedupe algorithm is generic, but repeated known-duplicate clusters are inherently repo-specific.
- `.agents/skills/update-pr-review/SKILL.md` and `.agents/skills/update-pr-review/scripts/aggregate_review_feedback.py` — today assume they can rewrite the core review skill bodies.

Prompt-construction entrypoints that currently assemble agent prompts:

- `.github/scripts/review_pr.py (285-389)` — assembles the review prompt and currently adds no repo-local skill context.
- `.github/scripts/triage_new_issues.py (55-79)` — `triage_heuristics_prompt(owner, repo)` hardcodes `warpdotdev/Warp` guidance in Python; this is the most explicit existing example of repo-tunable state living in the wrong layer.
- `.github/scripts/triage_new_issues.py (82-91)` — `fetch_command_signatures_context()` similarly hardcodes `warpdotdev/Warp`.
- `.github/scripts/triage_new_issues.py (229-340)` — the `process_issue()` prompt body, where a new repo-local section would be added.
- `.github/scripts/create_spec_from_issue.py`, `.github/scripts/create_implementation_from_issue.py`, `.github/scripts/respond_to_pr_comment.py`, `.github/scripts/respond_to_triaged_issue_comment.py` — will later benefit from the same pattern but are out of scope for the first migration except where trivially touched.
- `.github/scripts/update_pr_review.py` — currently tells the self-improvement loop to write to `.agents/skills/review-pr/SKILL.md` and `.agents/skills/review-spec/SKILL.md`; that prompt text needs to change.

Repo-specific state already separated from core skills (stays as-is):

- `.github/issue-triage/config.json`
- `.github/STAKEHOLDERS`
- `specs/GH*/`

Workflow wrappers that remain unchanged at the integration boundary:

- `.github/workflows/review-pull-request.yml`
- `.github/workflows/triage-new-issues.yml`
- `.github/workflows/update-pr-review.yml`
- `.github/workflows/update-pr-review-local.yml`

### Current state

Core skills hold a mix of universal contract and Warp/Oz-specific preferences. For example `triage-issue/SKILL.md (34-48)` enumerates issue-shape follow-up patterns that assume Warp's product surface area, and `review-pr/SKILL.md (27-41)` encodes Oz-specific user-facing-string norms (for example the phrasing rules around "The triage concluded that {summary}").

`triage_heuristics_prompt(owner, repo)` in `.github/scripts/triage_new_issues.py (55-79)` branches explicitly on `owner == "warpdotdev" and repo == "Warp"` and returns different prompt text. That is the single clearest signal that a repo-local layer is already needed; it just lives in Python today.

`update_pr_review.py` calls the agent with a prompt that names the core skill files directly as write targets, and `aggregate_review_feedback.py` lives inside `.agents/skills/update-pr-review/scripts/` and has no notion of a "repo-local" write surface.

Triage has no self-improvement loop; maintainer re-labels and overrides are not captured as learning signal.

`docs/platform.md (99-106)` documents the self-improvement agent but in terms of rewriting the core skills; the document needs to be updated once the pattern changes.

### Proposed changes

#### 1. Introduce a repo-local skill layer

Create new companion skills, each with YAML frontmatter and Markdown body:

- `.agents/skills/review-pr-local/SKILL.md`
- `.agents/skills/review-spec-local/SKILL.md`
- `.agents/skills/triage-issue-local/SKILL.md`
- `.agents/skills/dedupe-issue-local/SKILL.md`

Each companion file's frontmatter declares the core skill it specializes, for example:

```yaml
---
name: review-pr-local
specializes: review-pr
description: Repo-specific review guidance for oz-for-oss. Only the categories declared overridable by the core review-pr skill may be specialized here.
---
```

Move only the repo-specific rules out of the core skills and out of `triage_heuristics_prompt()` into the corresponding companion files. Specifically:

- `review-pr-local`: user-facing-string norms (section "User-facing strings"), graceful-degradation rules, debugging/observability rules currently in `review-pr/SKILL.md`.
- `review-spec-local`: repo-specific spec-section expectations and links to `specs/GH*/` conventions currently in `review-spec/SKILL.md`.
- `triage-issue-local`: the Warp-specific block from `triage_heuristics_prompt(owner, repo)` plus the `area:keyboard-layout` guidance and the issue-shape patterns from `triage-issue/SKILL.md`.
- `dedupe-issue-local`: seeded empty with a "no rules yet" body; gets populated over time by a future `update-dedupe` loop or by a reviewer.

Each core skill gets a short "Repository-specific overrides" section that explicitly enumerates the override categories the companion may specialize (for example "label taxonomy", "recurring follow-up patterns", "user-facing-string norms"). Categories not listed there are non-overridable.

#### 2. Shared helper for resolving the repo-local layer

Add a new helper in `.github/scripts/oz/helpers.py` (or a new `repo_local.py` module next to it) with this shape:

```python
def resolve_repo_local_skill_path(workspace: Path, core_skill_name: str) -> Path | None:
    """Resolve the repo-local companion skill path for a core skill.

    Returns the absolute path to `.agents/skills/<core_skill_name>-local/SKILL.md`
    in the consuming repository's workspace when the file exists and contains
    non-frontmatter body content; otherwise returns None.
    """
```

- The helper resolves `.agents/skills/<core_skill_name>-local/SKILL.md` relative to `workspace`, which is the consuming repository's checkout — never a path inside `oz-for-oss` itself.
- Missing file → returns `None`.
- File exists but has only YAML frontmatter or is otherwise empty → returns `None` so the prompt section is silently omitted.
- File exists with non-frontmatter body content → returns the resolved `Path`. The prompt builder is responsible for embedding that path by reference inside a fenced section; it must not inline the file contents.

Expose a second helper that emits the canonical fenced section as a path reference:

```python
def format_repo_local_prompt_section(core_skill_name: str, companion_path: Path) -> str:
    return (
        f"## Repository-specific guidance for `{core_skill_name}`\n"
        f"Read and follow the companion skill at `{companion_path}` in the "
        "consuming repository's checkout. Its guidance may override only the "
        "categories your core skill marks as overridable. It must not change "
        "the core skill's output schema, severity labels, or safety rules.\n"
    )
```

The formatted section intentionally contains only the path reference plus the override reminder. The agent loads the companion file itself via the usual skill-read path; the prompt builder never slurps its body into the prompt string.

Add unit tests under `.github/scripts/tests/test_repo_local.py` covering:

- missing file returns `None`
- empty file returns `None`
- frontmatter-only file returns `None`
- file with body returns the resolved path
- `format_repo_local_prompt_section` produces the fenced section containing the expected path reference and the override reminder, and does not contain the companion body

#### 3. Wire the prompt-construction layer

Update `.github/scripts/review_pr.py`:

- Call `resolve_repo_local_skill_path(workspace(), skill_name)` where `skill_name` is already computed at `.github/scripts/review_pr.py:335` and `workspace()` is the consuming repository's checkout.
- If non-None, append `format_repo_local_prompt_section(skill_name, companion_path)` to the `prompt` string built at `.github/scripts/review_pr.py (351-389)`, placed after the existing "Spec Context" block but before the "Cloud Workflow Requirements" block. The appended section is a path reference, not the companion body.
- Keep the rest of the prompt byte-for-byte identical when the companion file is absent.

Update `.github/scripts/triage_new_issues.py`:

- Remove the Warp-specific branch from `triage_heuristics_prompt(owner, repo)` at `.github/scripts/triage_new_issues.py (55-79)`; keep the function returning only the generic rules as the default base. The Warp-specific prose that used to live in that branch is expected to land in `warpdotdev/Warp`'s own `.agents/skills/triage-issue-local/SKILL.md`, not in `oz-for-oss`. The Python conditional removal must be coordinated so Warp's companion exists in its workspace before the conditional is removed from a released `oz-for-oss` ref; `oz-for-oss`'s own companion ships either with generic content or absent.
- In `process_issue()` at `.github/scripts/triage_new_issues.py (229-340)`, call `resolve_repo_local_skill_path(workspace(), "triage-issue")` and `resolve_repo_local_skill_path(workspace(), "dedupe-issue")` once per run and pass the results into prompt assembly.
- Add the fenced section(s) to the prompt using `format_repo_local_prompt_section("triage-issue", triage_local_path)` and `format_repo_local_prompt_section("dedupe-issue", dedupe_local_path)` when present.
- Leave `fetch_command_signatures_context()` untouched in this change; it is a separate repo-specific concern that is already structured as external repo data rather than skill prose, and re-shaping it would expand scope.

Add/extend tests under `.github/scripts/tests/`:

- A new test for `review_pr.py` that patches `resolve_repo_local_skill_path` to confirm the prompt includes the fenced path-reference section when the helper returns a path, and omits it when the helper returns `None`.
- A new test for `triage_new_issues.py` `process_issue()` prompt assembly asserting the same behavior for both the triage and dedupe companions.
- A test asserting the emitted fenced section contains only the companion path and the override reminder, never the companion body.

#### 4. Narrow the self-improvement write surface

Update `.agents/skills/update-pr-review/SKILL.md`:

- Replace references to `.agents/skills/review-pr/SKILL.md` / `review-spec/SKILL.md` with `.agents/skills/review-pr-local/SKILL.md` / `review-spec-local/SKILL.md`.
- Add an explicit "write surface" section that lists the only files the loop may write to and forbids touching core skill files or `.github/scripts/*`.

Update `.github/scripts/update_pr_review.py`:

- Update the dedented prompt to name the `-local` skill files as the write targets.
- Restructure the control flow so the Python entrypoint, not the agent, gates the push. Today the agent commits and pushes `oz-agent/update-pr-review`; that must change. Instruct the agent via its prompt to leave a local commit on `oz-agent/update-pr-review` without pushing and to exit once the commit is staged. Then run `git diff --name-only origin/main...oz-agent/update-pr-review` in `update_pr_review.py` and fail if any path is outside `.agents/skills/review-pr-local/` or `.agents/skills/review-spec-local/`. Only push the branch when the guard passes. `.github/issue-triage/` is intentionally excluded from this loop's write surface because the triage label taxonomy is a triage signal, not a review signal, and is owned by `update-triage`.
- After the guard passes and the branch is pushed, the Python entrypoint opens a pull request itself (via `gh pr create`, tagging `@captainsafia`) rather than relying on the agent to open the PR. The agent's prompt no longer has an "open a pull request" instruction; removing that step without the entrypoint taking it over would leave the branch pushed silently with no reviewer notified.

Factor the push/PR plumbing into `oz/repo_local.py` (or a new `oz/push_guard.py`) so `update_pr_review.py`, `update_triage.py`, and `update_dedupe.py` share one implementation of `branch_exists`, `changed_files_since_origin_main`, and `maybe_push_update_branch`. Each entrypoint then only declares its own `ALLOWED_PREFIXES`, PR title, and PR body; future guard-logic changes land in a single place.

Add a new script `.github/scripts/update_triage.py` modeled on `update_pr_review.py`:

- Aggregation: add `.agents/skills/update-triage/scripts/aggregate_triage_feedback.py` that queries the GitHub API for signals relevant to triage (issues triaged in the last N days, subsequent label changes by maintainers, re-opens, follow-up comments). Closed-as-duplicate signals are intentionally excluded and are handled by the separate `update-dedupe` loop described below. Output a temp JSON payload analogous to `aggregate_review_feedback.py`.
- Prompt: instruct the `update-triage` skill to propose minimum-viable edits to `.agents/skills/triage-issue-local/SKILL.md`, and to update `.github/issue-triage/config.json` only when a label taxonomy change is warranted.
- Branch/PR: branch `oz-agent/update-triage`, tag `@captainsafia` for review, reuse the same app-token, Oz-agent plumbing, and shared `maybe_push_update_branch` helper as `update_pr_review.py`. The entrypoint opens the PR itself via `gh pr create` after the push succeeds.
- Write-surface guard: same push-gating control flow as `update_pr_review.py`, with allowed prefixes `.agents/skills/triage-issue-local/` and `.github/issue-triage/`.

Add new skill `.agents/skills/update-triage/SKILL.md` and bundled aggregation script, following the shape of `.agents/skills/update-pr-review/`.

Add new workflow wrappers:

- `.github/workflows/update-triage.yml`: reusable wrapper modeled on `update-pr-review.yml`.
- `.github/workflows/update-triage-local.yml`: weekly schedule + workflow_dispatch, modeled on `update-pr-review-local.yml`.

Add a sibling `update-dedupe` loop that owns the closed-as-duplicate signal:

- `.github/scripts/update_dedupe.py` modeled on `update_triage.py` and using the same shared push/PR helper, with aggregation script `.agents/skills/update-dedupe/scripts/aggregate_dedupe_feedback.py`. The aggregator limits itself to issues GitHub itself recorded as closed with the duplicate close reason (`state_reason == "duplicate"`) and resolves each duplicate's canonical issue from the issue timeline's `marked_as_duplicate` event. Ad-hoc maintainer comments that merely mention another issue number (for example "see also #50") are intentionally ignored: pattern-matching on prose would feed false positives into the learning loop that the `update-dedupe` agent then has to reason away.
- Skill `.agents/skills/update-dedupe/SKILL.md` instructs the agent to propose minimum-viable edits to `.agents/skills/dedupe-issue-local/SKILL.md` only. Its write-surface guard allows only `.agents/skills/dedupe-issue-local/`.
- Workflow wrappers `.github/workflows/update-dedupe.yml` and `.github/workflows/update-dedupe-local.yml`, scheduled weekly with `workflow_dispatch`.

#### 5. Bootstrap behavior

Extend `.agents/skills/bootstrap-issue-config/SKILL.md`:

- Do **not** materialize empty `<agent>-local/SKILL.md` scaffolds during bootstrap. The prompt-construction layer already treats a missing file and a body-only frontmatter stub as equivalent, so an empty scaffold has no behavioral value and only adds review churn.
- Bootstrap only documents the companion directory convention (`<agent>-local/` for `review-pr`, `review-spec`, `triage-issue`, `dedupe-issue`). Each companion file is created on-demand by the matching `update-<agent>` self-improvement loop (or by a maintainer) the first time there is evidence-backed content to add.
- If a companion file already exists in the target repo, leave it untouched; bootstrap is additive.

#### 6. Documentation

Update `docs/platform.md`:

- Add a new section "Core skills and repo-local companions" that explains the split, lists the four initial companion skills, and describes the fenced prompt section convention.
- Update the "Self-improvement agent" section so it describes `update-pr-review` as writing only to `review-pr-local` / `review-spec-local`, and introduce the new `update-triage` role.
- Update the final summary sentence to mention the self-improvement roles in the plural.

### End-to-end flow

For a PR review run with repo-local guidance present:

1. `review-pull-request.yml` fires on PR event.
2. `review_pr.py` resolves `skill_name` (`review-pr` or `review-spec`) at `.github/scripts/review_pr.py:335`.
3. `review_pr.py` calls `resolve_repo_local_skill_path(workspace(), skill_name)`; returns the companion `Path` (in the consuming repo's checkout) or `None`.
4. `review_pr.py` appends a fenced "Repository-specific guidance" section to the prompt when non-None. The section references the companion path; it does not inline the companion body.
5. Oz runs the core `review-pr` (or `review-spec`) skill, reads the referenced companion as allowed-override context, and produces `review.json`.
6. `review_pr.py` posts the review to GitHub as before.

For a self-improvement run after the migration:

1. `update-triage-local.yml` fires weekly.
2. `update_triage.py` runs `aggregate_triage_feedback.py`, gets a JSON payload in `/tmp`.
3. `update_triage.py` invokes Oz with the `update-triage` skill, pointing it at `.agents/skills/triage-issue-local/SKILL.md` as its only write target.
4. Oz proposes minimal edits to that companion file (and optionally `.github/issue-triage/config.json`) and leaves the change as a local commit on `oz-agent/update-triage` without pushing.
5. `update_triage.py` runs the write-surface guard against `origin/main...oz-agent/update-triage`; a diff outside the allowed prefixes aborts the run. When the guard passes, the script pushes the branch.
6. PR is opened tagging `@captainsafia`.

`update-dedupe-local.yml` follows the same flow against closed-as-duplicate aggregation and writes only to `.agents/skills/dedupe-issue-local/SKILL.md`.

For a repo with no companion files yet:

1. The helper returns `None` for every companion.
2. No fenced section is added to the prompt.
3. Oz behaves exactly as if the split had not happened — core skill only.

### Risks and mitigations

- Regressing triage behavior on `warpdotdev/Warp` because the Warp-specific branch in `triage_heuristics_prompt()` is removed before `warpdotdev/Warp` ships its own companion. Mitigation: sequence the rollout so `warpdotdev/Warp`'s `.agents/skills/triage-issue-local/SKILL.md` lands first (populated with the Warp-specific prose formerly in the Python conditional), and only then remove the conditional from `oz-for-oss` in a release tagged for consumers to upgrade to. The regression test runs inside `warpdotdev/Warp` against its own companion file, not inside `oz-for-oss`. `oz-for-oss`'s companion ships either with generic content or is left absent so it does not leak Warp-specific guidance to other consumers.
- Self-improvement loops silently expanding their write surface. Mitigation: the Python entrypoint (`update_pr_review.py`, `update_triage.py`, `update_dedupe.py`) gates the push behind a `git diff` check against allowed prefixes. The agent leaves a local commit without pushing; the script pushes only if the guard passes.
- Companion files drifting out of sync with what the core skill marks overridable. Mitigation: the core skill explicitly lists override categories, and the companion template includes section headers matching those categories. Reviewers can catch non-conforming companions during PR review.
- Prompt bloat if a companion file is large. Mitigation: the fenced prompt section is a short path reference plus an override reminder; the companion body never lands in the prompt string. The agent reads the companion file directly via its usual skill-read path.
- Ambiguity about whether a companion file exists but is "effectively empty." Mitigation: `resolve_repo_local_skill_path` uses a strict check (non-frontmatter body length > 0 after trimming) and tests cover empty / frontmatter-only cases explicitly.
- The self-improvement loop running before the migration completes could regress because it still targets the old paths. Mitigation: land the migration and the `update_pr_review.py` prompt change in the same PR; disable the weekly schedule temporarily via `workflow_dispatch`-only until the PR is merged.

### Testing and validation

- New unit tests for the `resolve_repo_local_skill_path` and `format_repo_local_prompt_section` helpers, covering missing / empty / frontmatter-only / populated cases, and asserting the formatted section contains a path reference without inlining the companion body.
- Extended tests for `review_pr.py` prompt assembly and `triage_new_issues.py` `process_issue()` prompt assembly that patch the helper and assert the prompt includes/excludes the fenced reference section as expected.
- Cross-repo regression coverage lives in `warpdotdev/Warp` (not in `oz-for-oss`): a test that loads Warp's own `.agents/skills/triage-issue-local/SKILL.md` and asserts the contents match the Warp-specific prose previously returned by `triage_heuristics_prompt()` (up to whitespace and ordering). `oz-for-oss` tests assert only that the Python conditional is gone and that the helper correctly emits a reference when a companion file is present.
- Diff-guard test for `update_pr_review.py`, `update_triage.py`, and `update_dedupe.py` that simulates a proposed diff touching a core skill file and asserts the job fails before pushing.
- Manual validation via `workflow_dispatch`:
  - run `update-pr-review-local.yml` on a recent week's feedback and confirm the resulting PR diff is restricted to the repo-local skills
  - run `update-triage-local.yml` on a recent week and confirm the produced PR's diff is restricted to the triage companion and `.github/issue-triage/*`
  - run `update-dedupe-local.yml` on a recent week and confirm the produced PR's diff is restricted to `.agents/skills/dedupe-issue-local/`
  - run `review-pull-request.yml` against a PR both with and without `review-pr-local/SKILL.md` present and compare the prompts
- `docs/platform.md` is updated and builds (it is plain Markdown, so this is a read-review step).

### Follow-ups

- Evaluate whether `review-pr-local` and `review-spec-local` can be collapsed into a single `review-local` companion consumed by both core skills. Track as a follow-up spec once we have usage data.
- Extend the pattern to `create-spec-from-issue`, `create-implementation-from-issue`, and `respond-to-*` entrypoints as those accumulate repo-specific prompt guidance.
- Consider introducing `update-implementation` after the implementation agent has accumulated enough reviewer signal to justify the loop.
- Investigate factoring `fetch_command_signatures_context()` into a similar "repo-local data source" abstraction; out of scope for this change.
