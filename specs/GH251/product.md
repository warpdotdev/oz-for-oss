# Issue #251: Pull repo-tunable state out of core agents into repo-specific skills for self-improvement loops

## Product Spec

### Summary

The reusable agent skills in `oz-for-oss` (for example `review-pr`, `review-spec`, `triage-issue`, `dedupe-issue`) currently mix two very different things in one `SKILL.md` file: the stable cross-repo contract that every consumer of `oz-for-oss` should share, and the repository-specific preferences, heuristics, and taxonomies that only make sense in the current repo. Today `update-pr-review` — the only self-improvement loop we ship — rewrites those core skill files in place.

This change introduces a general pattern for splitting each reusable agent into a fixed core skill and a repository-specific companion layer, wires the prompt-construction scripts to include that companion layer as additional context at runtime, and scopes self-improvement loops so they only ever write to the repo-specific layer.

### Problem

As more repositories adopt `oz-for-oss`:

- Every automated edit to a core skill risks regressing a shared contract based on feedback from a single repo.
- Maintainers who want repo-specific review nits, triage taxonomies, or domain-specific follow-up patterns have no place to put them that survives upstream skill updates.
- Self-improvement is only defined for PR review; triage overrides, dedupe clusters, and other signals never feed back into the agent.
- Self-improvement PRs are large and hard to review because their write surface is effectively the entire skill file.

Repo-specific rules already exist, but they live in the wrong places: hardcoded `owner == "warpdotdev" and repo == "Warp"` branches inside `triage_new_issues.py`, one-off preferences glued onto the shared skill bodies, and ad-hoc prose mixed into each `SKILL.md`. That coupling prevents the shared skills from being truly reusable.

### Goals

- Each reusable agent skill in `.agents/skills/<agent>/SKILL.md` becomes a stable, cross-repo contract that self-improvement loops never rewrite.
- Every reusable agent that has repo-tunable behavior has a clearly defined repo-specific companion location where preferences, heuristics, and taxonomies live.
- The repo-specific layer is loaded as additional prompt context at runtime by the existing `.github/scripts/*` entrypoints and is clearly fenced so the agent knows which guidance is repo-specific.
- Each self-improvement loop only ever writes to its own repo-specific layer (and, for `update-triage` only, to `.github/issue-triage/*`), never to `.agents/skills/<agent>/SKILL.md` or `.github/scripts/*`. The `update-pr-review` loop in particular does not own the triage label taxonomy and must not write to `.github/issue-triage/`.
- A new `update-triage` self-improvement loop exists and updates repo-specific triage heuristics weekly, following the same shape as `update-pr-review`.
- A new `update-dedupe` self-improvement loop exists and consumes closed-as-duplicate signals specifically, writing only to the dedupe companion skill.
- The pattern is documented in `docs/platform.md` so other repos adopting `oz-for-oss` understand how to extend it.

### Non-goals

- Changing the agent-facing contract of any core skill's output (for example, `review.json` shape, `triage_result.json` shape, label output rules, suggestion-block constraints). Core contracts stay byte-for-byte compatible.
- Introducing self-improvement loops for `implement-issue` / `implement-specs`. Those are explicitly out of scope until there is a clearer learning signal.
- Rewriting the existing reusable workflow wrappers in `.github/workflows/`. The integration boundary for other systems stays the same.
- Replacing `.github/issue-triage/config.json` or `.github/STAKEHOLDERS`. Those remain the structured repo-specific state; the new repo-local skills are for prose guidance that does not fit those schemas.
- Building a discovery UI for repo-specific skills, or renaming upstream skills.

### Figma / design references

Figma: none provided. This is a workflow and repository-layout change with no UI beyond GitHub issue and PR comments.

### User experience

This feature has three primary user groups: maintainers of `oz-for-oss`, maintainers of a repository that consumes `oz-for-oss`, and Oz itself acting through the reusable agents.

#### Repository layout

For each reusable agent role with tunable state, the repository layout is:

- `.agents/skills/<agent>/SKILL.md` — the core skill. Stable cross-repo contract. Self-improvement loops never write here.
- `.agents/skills/<agent>-local/SKILL.md` — the repo-specific companion skill. Self-improvement loops write here. The file is plain Markdown with a YAML frontmatter block just like other skills, so the directory is itself a valid skill discovery target.

The agents that get a companion layer in this change are at minimum:

- `review-pr` → `review-pr-local`
- `review-spec` → `review-spec-local`
- `triage-issue` → `triage-issue-local`
- `dedupe-issue` → `dedupe-issue-local`

If a repo has no repo-specific guidance for an agent, the companion skill file may be absent. The prompt-construction layer must handle that case silently.

#### Core skill contract

The core skill must clearly state:

- what parts of its behavior are fixed contract (output JSON schema, severity labels, safety rules, evidence rules, ordering of mandatory steps)
- what parts MAY be specialized by the repo-specific companion (label taxonomy, repo-specific style nits, owner-inference hints, recurring duplicate clusters, domain-specific follow-up question patterns, allowlists of paths to skip)

A core skill must never silently defer to the companion. Repo-specific overrides only apply to areas the core explicitly marks as overridable.

#### Repo-specific companion skill contract

Each `<agent>-local/SKILL.md` must:

- declare which core skill it specializes, by name
- group its guidance under the categories the core skill marks as overridable
- be self-contained Markdown that a human can read top-to-bottom without needing to cross-reference the core skill

#### Prompt construction behavior

When a workflow invokes a reusable agent, the script at `.github/scripts/<script>.py` must:

- detect whether the companion skill file exists in the consuming repository's workspace
- if present, include a clearly fenced section in the prompt that *references the companion file by path* (for example `.agents/skills/review-pr-local/SKILL.md` resolved against the consuming repository's checkout), and instructs the agent to read and follow that file, rather than inlining its contents into the prompt
- name the core skill the companion specializes and the specific override categories
- omit the section entirely if the companion file does not exist

The referenced path must always point at the companion file in the consuming repository's workspace, never at a companion file that happens to ship inside `oz-for-oss`. If the companion file exists but is empty or only contains the frontmatter scaffold, the script must treat it as absent rather than emitting a reference to an effectively empty file.

#### Self-improvement loop behavior

Each `update-<agent>` self-improvement loop:

- aggregates a time-windowed set of GitHub signals (review comments, label changes, maintainer comments, re-opens) into a temporary JSON payload. Closed-as-duplicate signals are the sole input for the `update-dedupe` loop and feed that loop specifically; they do not feed `update-triage`.
- classifies those signals by which repo-specific skill they should update
- proposes minimum-viable edits to the repo-specific skill only
- opens a PR against branch `oz-agent/update-<agent>` with those edits for human approval

The loop must skip producing a PR when there is no repeated signal. "Repeated" means the same repo-specific pattern is corroborated by at least two independent threads or a single explicit maintainer statement. One-off reviewer preferences must not be encoded as rules.

Each loop's allowed write surface is strictly scoped to the files it owns:

- `update-pr-review`: `.agents/skills/review-pr-local/` and `.agents/skills/review-spec-local/`
- `update-triage`: `.agents/skills/triage-issue-local/` and `.github/issue-triage/*`
- `update-dedupe`: `.agents/skills/dedupe-issue-local/`

Writes outside that surface — in particular to `.agents/skills/<agent>/SKILL.md` or `.github/scripts/*` — must fail fast rather than silently continue. Two loops must never share ownership of the same file; for example, the triage label taxonomy (`.github/issue-triage/*`) is owned by `update-triage` only.

#### Invariants

- The core `SKILL.md` of a shared agent is byte-for-byte identical across repos consuming `oz-for-oss` at the same ref, except for the repo-specific companion it references.
- The companion skill never redefines the agent's output schema, severity labels, safety rules, or core evidence requirements.
- The self-improvement loop is a pure function of signals-in → companion-skill-out; it never reaches into workflow workflows.
- An absent or empty companion file is a supported state, not an error.
- Running `update-<agent>` when no repeated signal exists produces no branch and no PR.

#### Edge cases

- A consuming repository adopts `oz-for-oss` for the first time and has no companion files. All agents must run correctly from the core contract alone.
- The companion file contains contradictory guidance with the core contract. The core contract wins and the agent should behave exactly as if the contradicting lines were absent.
- A self-improvement loop runs in a repository that has not created any companion files yet. The loop must create the companion file at the documented path (including a frontmatter scaffold) when it has evidence-backed content to add, and must do nothing otherwise.
- The self-improvement loop observes signals that would change the core contract (for example, "the output JSON shape should be different"). It must ignore those signals rather than proposing unrelated edits to the companion file or silently weakening the contract.
- Two self-improvement PRs are open at once for the same companion file. The usual PR review process resolves the conflict; the loop itself must not force-push or close existing PRs.

#### Success criteria

- `.agents/skills/review-pr/SKILL.md`, `.agents/skills/review-spec/SKILL.md`, `.agents/skills/triage-issue/SKILL.md`, and `.agents/skills/dedupe-issue/SKILL.md` contain only cross-repo contract content; they explicitly enumerate the categories a companion skill may override.
- Companion skills exist at `.agents/skills/review-pr-local/SKILL.md`, `.agents/skills/review-spec-local/SKILL.md`, `.agents/skills/triage-issue-local/SKILL.md`, and `.agents/skills/dedupe-issue-local/SKILL.md` containing the repo-specific rules previously embedded in the core skills or hardcoded in Python.
- `review_pr.py` includes the matching companion file in the review prompt when it exists, fenced and labeled as repo-specific, and silently omits it when absent.
- `triage_new_issues.py` reads `triage-issue-local` and `dedupe-issue-local` companion files instead of hardcoding per-owner/repo guidance. The existing Warp-specific branch in `triage_heuristics_prompt()` is removed from Python; the prose it used to return lands in `warpdotdev/Warp`'s own `.agents/skills/triage-issue-local/SKILL.md` (not in `oz-for-oss`), and the Python conditional is removed only once that companion is in place. `oz-for-oss`'s own companion ships with generic content or remains absent so it does not leak Warp-specific guidance to other consumers.
- `update-pr-review` writes only to `.agents/skills/review-pr-local/SKILL.md` and `.agents/skills/review-spec-local/SKILL.md`. Any attempt to write to the core skill files fails.
- A new `update-triage` loop is scheduled weekly (mirroring `update-pr-review-local.yml`) and, when it produces a PR, the diff touches only `.agents/skills/triage-issue-local/SKILL.md`, `.agents/skills/dedupe-issue-local/SKILL.md`, or files under `.github/issue-triage/`.
- `docs/platform.md` documents the pattern, with a section explaining the core-vs-local split and listing the existing companion skills.
- Running a reusable agent in a repository with no companion files produces the same behavior as before the split (no regressions in the default path).

### Validation

- Unit-level: add tests under `.github/scripts/tests/` that exercise the prompt-construction layer with and without a companion file, including the empty-file case and a missing-file case.
- Integration-level: run `triage_new_issues.py` against a fixture that exercises `warpdotdev/Warp` heuristics via a companion file present in the consuming workspace and assert the Warp-specific guidance is referenced in the prompt without any Python conditional.
- Self-improvement write surface: add a test for `update-pr-review` and the new `update-triage` loop that asserts the diff produced by the loop only touches files under `.agents/skills/*-local/` or `.github/issue-triage/`, and fails otherwise.
- Manual validation: run `update-pr-review-local.yml` via `workflow_dispatch` once after the migration and confirm the resulting PR diff matches the narrowed write surface.
- Manual validation: run `update-triage` via `workflow_dispatch` at least once against a week where maintainers overrode triage output, and confirm the PR's diff is restricted to the companion files.
- Regression check: for each migrated agent, diff the prompt emitted before and after the split against a representative fixture; the resulting combined prompt should be equivalent to the pre-split prompt up to ordering and the new fenced reference section.

### Open questions

- Whether the repo-specific layer should be shared across related agents (for example a single `review-local` skill consumed by both `review-pr` and `review-spec`) or kept strictly per-agent.
- How to bootstrap the repo-specific layer when a repository first adopts `oz-for-oss`. Current assumption: bootstrap does not materialize empty companion files. The prompt-construction layer already treats a missing file and a body-only frontmatter stub as equivalent, so the directories stay absent until the matching `update-<agent>` loop (or a maintainer) lands a file with real content.
- Whether an `update-implementation` loop is worth shipping. This spec defers it until there is clearer signal.
