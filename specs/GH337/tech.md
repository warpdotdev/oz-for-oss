# Issue #337: Fix PR-issue linking detection with a single canonical association standard

## Tech Spec

### Problem

PR association in oz-for-oss is currently split across incompatible implementations:

- `.github/workflows/remove-stale-issue-labels-on-plan-approved.yml` uses inline JavaScript and a raw `/#(\d+)/g` scan over the PR body, which is unsafe for any workflow that mutates issue state.
- `.github/scripts/oz/helpers.py` uses `ISSUE_PATTERN` plus same-repo issue URLs, which still reparses PR-body text instead of using GitHub’s own linked-issue model.
- `resolve_issue_number_for_pr()` mixes branch and spec-path candidates with parsed-body candidates, which is acceptable for deterministic branch conventions but too weak as the canonical strategy for all PR association.

The product spec requires one shared resolver that prefers deterministic Oz conventions and GitHub-native linked issue data only, and makes destructive workflows safe under ambiguity.

### Relevant code

- `.github/scripts/oz/helpers.py:30` — current `ISSUE_PATTERN` definition.
- `.github/scripts/oz/helpers.py (1468-1507)` — `extract_issue_numbers_from_text()` and `resolve_issue_number_for_pr()`, which are the closest thing to a shared resolver today.
- `.github/scripts/enforce_pr_issue_state.py (1-98)` — uses `extract_issue_numbers_from_text()` to find an explicit associated issue before deciding whether to allow or close a contributor PR.
- `.github/workflows/remove-stale-issue-labels-on-plan-approved.yml (27-83)` — inline `actions/github-script` logic that currently scans any `#123` token from the PR body.
- `.github/scripts/trigger_implementation_on_plan_approved.py (1-62)` — another current consumer of `resolve_issue_number_for_pr()`, so any shared resolver change needs to preserve spec-PR behavior.
- `.github/scripts/tests/test_helpers.py (24-33, 684-723)` — existing tests for `extract_issue_numbers_from_text()` and `resolve_issue_number_for_pr()`.
- `.github/scripts/tests/test_enforce_pr_issue_state.py (1-293)` — current enforcement tests that assume text parsing is the only explicit association signal.

### Current state

Today the repo has three classes of PR-to-issue signals:

1. **Deterministic Oz-managed signals**
   - branch names like `oz-agent/spec-issue-337`
   - changed files like `specs/GH337/product.md`
2. **PR body parsing**
   - helper-side regex parsing via `ISSUE_PATTERN`
   - workflow-side raw `/#(\d+)/g` parsing
3. **GitHub-native issue links**
   - not currently queried by repo code, even though GitHub’s GraphQL API exposes `closingIssuesReferences` for closing-keyword associations and timeline events can be used to reconstruct manual linked-issue connections

That split leads to two concrete implementation problems:

- The stale-label workflow duplicates logic in YAML/JavaScript instead of calling shared code, so it drifted into a dangerously broad parser.
- The enforcement workflow depends on the helper-side parser alone, so it ignores authoritative same-repo linked-issue data that GitHub already exposes for the PR.

The repo already uses PyGithub and already performs one GraphQL mutation in `helpers.py`, so adding a small GraphQL query helper beside the existing helpers is consistent with current stack choices.

### Proposed changes

#### 1. Introduce a canonical PR-association helper in Python

Add a richer helper layer in `.github/scripts/oz/helpers.py` that separates:

- **candidate collection** from
- **primary issue selection** from
- **workflow-specific policy**

The helper should produce structured association results rather than only a flat list of numbers. A concrete shape can be:

```python
{
    "deterministic_issue_numbers": [337],
    "github_linked_issues": [
        {"owner": "warpdotdev", "repo": "oz-for-oss", "number": 337, "source": "closingIssuesReferences"},
    ],
    "same_repo_issue_numbers": [337],
    "primary_issue_number": 337 | None,
    "ambiguous": False,
}
```

That lets destructive workflows use `primary_issue_number` while gating workflows can inspect the full `same_repo_issue_numbers` set.

#### 2. Query GitHub-native linked issue data as the only PR-level source of truth

Add a GraphQL query helper that fetches linked issue data for a PR in two ways:

1. **`closingIssuesReferences`**
   - authoritative for issues GitHub already understands as linked via supported closing keywords
   - returns repository context, which avoids assuming every linked issue is same-repo
2. **timeline connection events**
   - query `ConnectedEvent` and `DisconnectedEvent` on the PR timeline (or equivalent resource lookup) to reconstruct the currently connected manual linked issues from the PR sidebar

The merged result should:

- preserve repository owner/name along with the issue number
- filter down to same-repository issues for oz-for-oss workflows
- deduplicate against deterministic branch/spec-path candidates
- tolerate pagination for `closingIssuesReferences` when GitHub returns more than one page

This gives the resolver an authoritative PR-level source of truth without relying on brittle body parsing.

#### 3. Stop using PR-body text parsing for PR association

Remove `extract_issue_numbers_from_text()` from PR-association decisions. Concretely:

- do not treat incidental bare `#123` mentions as associations
- do not treat softer phrases like `Addresses #N`, `Related to #N`, `Part of #N`, or `Towards #N` as associations
- do not treat even keyword phrases like `Closes #N` as a direct signal unless GitHub already surfaces the relationship in linked-issue data
- keep `extract_issue_numbers_from_text()` only if another non-association call site still needs it; otherwise delete or deprecate it as part of the refactor

This keeps the resolver aligned with one authoritative model instead of maintaining a second repository-defined association language.

#### 4. Split “find all associated issues” from “pick a single issue safely”

Refactor the current `resolve_issue_number_for_pr()` usage into two explicit operations:

- **association resolution**: returns all associated same-repo issue candidates plus metadata about how they were found
- **primary issue resolution**: returns exactly one issue number only when the selection is deterministic and safe

Selection rules:

- deterministic Oz signals win over GitHub-linked issue data when they identify a single same-repo issue
- if exactly one same-repo GitHub-linked issue exists, it becomes the primary issue
- if multiple same-repo issues exist and no higher-confidence deterministic signal breaks the tie, `primary_issue_number` is `None`

This keeps destructive workflows safe while still giving non-destructive workflows access to the full candidate set.

#### 5. Move stale-label removal out of inline JavaScript and into shared Python

Replace the inline `actions/github-script` implementation in `.github/workflows/remove-stale-issue-labels-on-plan-approved.yml` with a Python entry point, for example:

- `.github/scripts/remove_stale_issue_labels_on_plan_approved.py`

That script should:

1. load the PR
2. compute association using the canonical helper
3. exit without mutating anything unless `primary_issue_number` is set
4. fetch the target issue and remove `ready-to-spec` only from that one issue

Using Python here is the simplest way to guarantee the workflow consumes the same resolver as the rest of the repo instead of carrying a second copy in JavaScript.

#### 6. Update contributor PR enforcement to consume the richer association result

Refactor `.github/scripts/enforce_pr_issue_state.py` so that it:

1. resolves associated same-repo issues via the canonical helper
2. fetches all associated same-repo issues when any exist
3. allows the PR immediately if any associated issue has `ready-to-implement`
4. closes with the existing docs link if associated issues exist but none are ready
5. preserves the current agent-based fuzzy match fallback only when no associated same-repo issues are found at all

This change keeps the current “ready issue required” policy while reducing false closures caused by ignoring GitHub-native linked issue data.

#### 7. Preserve existing spec-PR and implementation-PR behavior

`resolve_issue_number_for_pr()` is currently used by spec-oriented automation such as `trigger_implementation_on_plan_approved.py`, where branch naming and spec-path conventions are already reliable. The new resolver must preserve that behavior:

- `oz-agent/spec-issue-337` should still resolve to `#337` even if the PR body includes other issue references
- `specs/GH337/product.md` and `specs/GH337/tech.md` should still resolve to `#337`
- these deterministic signals should remain higher priority than GitHub-linked issue data

### End-to-end flow

#### Flow A: stale-label removal on a spec PR

1. Workflow loads PR `#X`.
2. Shared resolver inspects deterministic signals:
   - branch name
   - changed spec paths
3. If needed, shared resolver reads GitHub-linked issue data.
4. Resolver returns `primary_issue_number=337`.
5. Script removes `ready-to-spec` only from issue `#337`.
6. If resolver returns `primary_issue_number=None`, script logs and exits with no mutation.

#### Flow B: contributor PR enforcement

1. Workflow loads PR `#Y`.
2. Shared resolver gathers same-repo associated issues from deterministic signals and GitHub-linked issue data.
3. If at least one same-repo associated issue exists:
   - fetch those issues
   - allow the PR if any are `ready-to-implement`
   - otherwise close with the existing guidance comment
4. If no same-repo association exists:
   - keep the current fuzzy “likely matching ready issue” agent flow

### Risks and mitigations

**Risk: GitHub GraphQL query failures or rate limits**
Mitigation: deterministic branch/spec-path signals are checked first. If no deterministic signal exists and linked-issue data cannot be fetched reliably, destructive workflows still no-op and non-destructive enforcement falls back to the existing fuzzy ready-issue matching path rather than inferring association from text.

**Risk: manual linked-issue reconstruction from timeline events is more complex than `closingIssuesReferences`**
Mitigation: isolate that logic in one helper that returns normalized issue records and hides event-level bookkeeping from callers. Add unit tests that cover connected/disconnected event pairs.

**Risk: multiple same-repo linked issues remain ambiguous**
Mitigation: destructive workflows consume `primary_issue_number` and no-op when it is absent. Non-destructive workflows use the full associated set.

**Risk: contributors may continue using text-only phrases that are no longer treated as explicit association**
Mitigation: update contributor guidance to recommend GitHub manual linking or official closing keywords, and keep the current fuzzy matching flow as a safety net only when no authoritative same-repo association exists.

**Risk: cross-repository links accidentally satisfy oz-for-oss readiness rules**
Mitigation: carry repository context through the GraphQL result and filter to same-repository issues before gating or mutating labels.

### Testing and validation

- Add helper tests for:
  - deterministic branch and spec-path resolution
  - `closingIssuesReferences` normalization
  - connected/disconnected manual-link timeline reconstruction
  - same-repo filtering
  - rejection of incidental bare `#123`
  - rejection of text-only phrases like `Addresses #123` when no GitHub-linked issue data exists
  - ambiguous multi-issue cases returning no primary issue
- Add enforcement tests for:
  - allowing a PR when a GitHub-linked same-repo issue is `ready-to-implement`
  - closing a PR when associated issues exist but none are ready
  - preserving the fuzzy agent path when no associated issues exist, including text-only phrases that do not produce GitHub-linked issue data
- Add stale-label tests for:
  - ignoring unrelated `#123` mentions in a spec PR body
  - removing `ready-to-spec` from the deterministic issue only
  - no-op on ambiguous association

### Follow-ups

- Update `CONTRIBUTING.md` to recommend GitHub-native linked issues or official closing keywords as the clearest contributor syntax for explicit PR association.
- Consider emitting lightweight logging or metrics for whether a PR’s association came from deterministic signals or GitHub-linked issue data so the team can measure how often each path is used.
