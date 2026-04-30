# Issue #337: Fix PR-issue linking detection with a single canonical association standard

## Product Spec

### Summary

PR-to-issue association in this repo should follow one canonical standard everywhere. Today one path treats any `#123` mention as an associated issue, while another reparses a narrow set of issue-closing phrases from the PR body. The result is the worst of both worlds: destructive workflows can mutate the wrong issue, and contributor PRs can still be auto-closed as “unlinked” even when GitHub already exposes authoritative linked-issue data.

The new behavior should prefer authoritative GitHub-linked issue data, preserve Oz’s deterministic branch and spec-path conventions, and stop using PR-body text parsing as a source of truth for PR association.

### Problem

The repo currently has two related failure modes:

1. **Too broad for destructive workflows.** The `remove-stale-issue-labels-on-plan-approved.yml` workflow scans the PR body with `/#(\d+)/g`, so any incidental `#123` mention can be treated as the associated issue. If the wrong issue happens to exist, the workflow may remove `ready-to-spec` from that unrelated issue.
2. **Wrong source of truth for contribution gating.** `.github/scripts/oz/helpers.py` reparses PR body text instead of consulting GitHub-native linked issue data. That means repo automation can disagree with GitHub’s own PR-to-issue association model and can miss legitimate same-repo links that GitHub already knows about, such as manually linked issues from the PR sidebar.

These failure modes are symptoms of the same product problem: the repo does not have a single, documented definition of what counts as an associated issue for a PR.

### Goals

- Define one canonical PR-to-issue association standard that all workflows use.
- Prefer authoritative, GitHub-native linked issue data instead of re-parsing freeform text in multiple places.
- Continue to recognize the repo’s deterministic Oz-managed conventions, such as `oz-agent/spec-issue-{N}` branches and `specs/GH{N}/...` paths.
- Eliminate PR-body text parsing as a repository-defined association mechanism.
- Make ambiguity safe: workflows must not guess and must not mutate unrelated issues.

### Non-goals

- Redesigning the repo’s readiness policy (`ready-to-spec` and `ready-to-implement`).
- Treating any bare `#123` mention in prose, code snippets, or changelog text as a valid association.
- Supporting text-only phrases such as `Addresses #123`, `Related to #123`, `Part of #123`, or `Towards #123` unless GitHub itself exposes them as linked issue data.
- Using cross-repository linked issues to satisfy same-repository gating or label-removal workflows.
- Changing commit-message-based issue linking behavior. This work is scoped to PR association used by repo automation.

### Figma / design references

Figma: none provided. This is a workflow and automation change with no product UI beyond GitHub issue, PR, and label behavior.

### User experience

#### Scenario: spec PR mentions multiple issue numbers in its body

1. A spec PR is opened for issue `#337`.
2. The body includes the real association (`Closes #337`) but also mentions other issues such as `See #323` and `See #324`.
3. When `plan-approved` is added and the stale-label workflow runs, the system identifies the canonical associated issue rather than scanning every `#123` token.
4. `ready-to-spec` is removed only from `#337`.
5. No unrelated issue label is changed.

#### Scenario: contributor PR uses an official GitHub-linked issue

1. A contributor opens a PR and links issue `#123` using supported GitHub closing keywords or GitHub’s manual linked-issue UI.
2. The repo’s enforcement workflow reads the PR’s associated issue from GitHub-linked issue data.
3. If issue `#123` is marked `ready-to-implement`, the PR is treated as linked and is not auto-closed for missing issue association.

#### Scenario: contributor PR uses a text-only phrase such as `Addresses #123`

1. A contributor opens a PR whose body says `Addresses #123`.
2. GitHub-linked issue data does not include that relationship because the phrase is not a GitHub-native association signal.
3. The repo does not treat the text alone as an explicit associated issue.
4. Contribution gating proceeds as if the PR is unlinked unless another deterministic or GitHub-native association signal exists.

#### Scenario: PR body contains an incidental `#123` mention only

1. A PR body mentions `#123` inside prose, a code block, or a “see also” list without a GitHub-native link.
2. The system does not treat that bare mention as an associated issue.
3. Destructive workflows make no issue mutation based on that mention.
4. Contribution gating continues to behave as if the PR is unlinked unless another supported association signal exists.

#### Scenario: multiple associated same-repository issues exist

1. A PR resolves to multiple same-repository associated issues via branch conventions or GitHub-linked issue data.
2. Workflows that require a single target issue to mutate state, such as stale-label removal, only proceed if one primary issue can be determined safely and deterministically.
3. If a single primary issue cannot be determined, the destructive workflow does nothing rather than guessing.
4. Contribution gating may still treat the PR as associated if at least one associated same-repository issue is marked `ready-to-implement`.

#### Behavior rules

1. **All PR association decisions use one shared standard.** The repo must not maintain separate “broad” and “narrow” linking rules in different workflows.
2. **Association prefers deterministic signals first.** For Oz-managed PRs, branch naming and changed spec paths remain valid high-confidence signals.
3. **GitHub-linked issue data is the primary PR-level source of truth.** When GitHub exposes linked issue data for the PR, repo automation should use that rather than ad hoc text parsing.
4. **PR-body text does not define association.** Bare `#123` mentions and softer phrases such as `Addresses #123` do not count unless GitHub itself exposes a linked issue relationship for the PR.
5. **Bare `#123` does not count.** A raw hash-number token in prose, code snippets, or “see also” lists is not treated as a linked issue.
6. **Cross-repository issues do not satisfy repo gating.** A PR linked to an issue in another repository may still be displayed as linked data, but oz-for-oss gating and label workflows only act on same-repository issues.
7. **Destructive workflows require a safe target.** Any workflow that removes or edits labels on an issue must act only when it can resolve a single safe target issue.
8. **Contribution gating optimizes for avoiding false auto-closures.** If at least one associated same-repository issue is `ready-to-implement`, the PR should not be auto-closed for missing issue association.

### Success criteria

1. The plan-approved stale-label workflow never removes `ready-to-spec` from an unrelated issue solely because that issue number appeared somewhere in the PR body.
2. The enforcement workflow recognizes PRs linked via GitHub-native linked issue data and does not auto-close them as “unlinked.”
3. The enforcement workflow does not infer an explicit association from text-only phrases such as `Addresses #N`, `Related to #N`, `Part of #N`, or `Towards #N` unless GitHub-native linked issue data also exists.
4. A bare `#N` mention without GitHub-native linked issue data is ignored consistently across workflows.
5. Two different code paths in the repo do not disagree about whether the same PR is associated with the same issue.
6. Destructive workflows no-op on ambiguous association rather than mutating the wrong issue.

### Validation

- Add unit tests for the canonical association resolver covering deterministic Oz signals, GitHub-linked issue data, incidental bare `#N` mentions, unsupported text-only phrases, and ambiguity handling.
- Add regression tests for the stale-label workflow path showing that unrelated issue references in a spec PR body do not cause the wrong issue label to be removed.
- Add regression tests for contributor PR enforcement showing that GitHub-linked same-repo issues are honored and that unsupported text-only phrases are not treated as explicit associations.
- Validate that cross-repository linked issues are ignored for oz-for-oss readiness gating and label mutation.
- Validate that existing branch-based and spec-path-based association for Oz-generated PRs continues to work unchanged.

### Open questions

1. Should the close comment for an unlinked contributor PR explicitly recommend GitHub’s manual linked-issue UI or official closing keywords as the preferred syntax for explicit association?
2. Should the repo surface when a PR was accepted through deterministic Oz-managed signals instead of GitHub-linked issue data, so maintainers can distinguish those paths in logs or metrics?
