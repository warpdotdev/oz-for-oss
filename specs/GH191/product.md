# Issue #191: Workflows should tolerate plan being approved after ready-to-implement label added

## Product Spec

### Summary

When `plan-approved` is added to a spec PR after `ready-to-implement` was already added to the associated issue, the implementation workflow should be triggered. Today, the implementation workflow only fires on the `ready-to-implement` label event and no-ops if the spec PR is not yet approved, leaving the issue stuck with no retry.

### Problem

The current issue lifecycle assumes a specific label ordering:

1. `ready-to-spec` → agent writes spec PR
2. Reviewer adds `plan-approved` to the spec PR
3. Reviewer adds `ready-to-implement` to the issue → agent starts implementation

If a reviewer adds `ready-to-implement` to the issue *before* adding `plan-approved` to the spec PR, the implementation workflow fires, discovers unapproved spec PRs, posts a no-op comment ("I did not start implementation because linked spec PR(s) exist for this issue but none are labeled `plan-approved`"), and stops. When `plan-approved` is later added to the spec PR, nothing triggers the implementation workflow again. The issue is permanently stuck until a human manually re-triggers the workflow.

This is a common enough sequencing mistake that the system should handle it gracefully.

### Goals

- When `plan-approved` is added to a spec PR, automatically find the associated issue and trigger the implementation workflow if that issue already has `ready-to-implement`.
- The implementation that runs should behave identically to one triggered by adding `ready-to-implement` — same spec context resolution, same branch naming, same PR creation behavior.
- No duplicate implementation runs: if the implementation workflow already ran successfully (i.e., the no-op case did not occur or has been resolved), the `plan-approved` trigger should not create a redundant run.

### Non-goals

- Changing the happy-path label ordering. The preferred sequence (`plan-approved` → `ready-to-implement`) remains the recommended workflow.
- Handling other out-of-order label scenarios beyond the `ready-to-implement` before `plan-approved` case.
- Changing the behavior of the spec creation workflow (`create-spec-from-issue`).
- Retrying other kinds of implementation failures (e.g., agent errors, transport timeouts).
- Automatically adding `plan-approved` to spec PRs.

### Figma / design references

Figma: none provided. This is a backend/workflow change with no UI beyond GitHub issue comments and labels.

### User experience

#### Scenario: `ready-to-implement` added before `plan-approved`

1. An issue has a linked spec PR on branch `oz-agent/spec-issue-{N}`.
2. A reviewer adds `ready-to-implement` to the issue.
3. The implementation workflow fires, finds the unapproved spec PR, and posts a no-op comment: "I did not start implementation because linked spec PR(s) exist for this issue but none are labeled `plan-approved`: #X".
4. The reviewer later adds `plan-approved` to the spec PR.
5. **New behavior**: The system detects this event, finds the associated issue, confirms it has `ready-to-implement`, and triggers the implementation workflow.
6. The implementation workflow runs with the now-approved spec context and produces an implementation PR.

#### Scenario: `plan-approved` added before `ready-to-implement` (unchanged)

1. A reviewer adds `plan-approved` to the spec PR.
2. **New behavior**: The system detects this event, finds the associated issue, but the issue does not have `ready-to-implement`. No implementation workflow is triggered.
3. Later, the reviewer adds `ready-to-implement` to the issue. The existing trigger fires and the implementation workflow runs normally with the approved spec context.

#### Scenario: No linked issue found

1. `plan-approved` is added to a spec PR.
2. The system cannot determine the associated issue from the PR branch name, changed files, or body. No implementation workflow is triggered. No error is posted.

#### Scenario: `plan-approved` added to a non-spec PR

1. `plan-approved` is added to a PR that is not a spec PR (i.e., not on an `oz-agent/spec-issue-{N}` branch and not modifying files under `specs/`).
2. No implementation workflow is triggered. This guarantee holds even if the non-spec PR body references an issue number (for example, `Fixes #123`) that otherwise would resolve to an issue with `ready-to-implement`.

#### Behavior rules

1. **Trigger on `plan-approved` label added to a PR.** When the `plan-approved` label is added to a pull request, the system looks up the associated issue number.
2. **Only spec PRs qualify.** Before resolving the associated issue, the trigger verifies the PR is a spec PR. A PR qualifies if its head branch matches the `oz-agent/spec-issue-{N}` pattern or if every changed file lives under `specs/`. Non-spec PRs are skipped even if their body references an issue number.
3. **Issue association uses existing logic.** For qualifying spec PRs, the associated issue is determined from the PR branch name (e.g., `oz-agent/spec-issue-{N}`), changed spec files (e.g., `specs/GH{N}/product.md`), and PR body references. This is the same logic used by `resolve_issue_number_for_pr`.
4. **Only trigger if `ready-to-implement` is present.** The implementation workflow is only dispatched if the associated issue has the `ready-to-implement` label and `oz-agent` is assigned. If either condition is missing, no workflow runs.
5. **Identical implementation behavior.** The triggered implementation workflow behaves identically to one triggered by adding `ready-to-implement`. It uses the same spec context resolution, branch naming, and PR creation logic.
6. **Bot and automation users are ignored.** If `plan-approved` was added by a bot or automation user, no workflow runs. This prevents infinite loops if automation manages labels.
7. **The `plan-approved` trigger only fires for open PRs.** If the spec PR is closed or merged when `plan-approved` is added, no workflow runs.

### Success criteria

1. When `plan-approved` is added to a spec PR whose associated issue has `ready-to-implement` and `oz-agent` assigned, the implementation workflow runs and produces an implementation PR.
2. When `plan-approved` is added to a spec PR whose associated issue does not have `ready-to-implement`, no implementation workflow runs.
3. When `plan-approved` is added to a non-spec PR or a PR with no identifiable associated issue, no implementation workflow runs.
4. The implementation produced via the `plan-approved` trigger is identical in quality and behavior to one triggered via `ready-to-implement` with an already-approved spec.
5. The existing `ready-to-implement` trigger continues to work as before with no regressions.
6. No duplicate implementation runs occur when both triggers fire in sequence.

### Validation

- **Workflow trigger test**: Add `plan-approved` to a spec PR whose associated issue has `ready-to-implement`. Confirm the implementation workflow is dispatched.
- **No-trigger test**: Add `plan-approved` to a spec PR whose associated issue does not have `ready-to-implement`. Confirm no implementation workflow runs.
- **No-trigger for non-spec PR**: Add `plan-approved` to a non-spec PR. Confirm no implementation workflow runs.
- **Regression test**: Confirm the standard happy path (`plan-approved` → `ready-to-implement`) still works correctly.
- **Unit tests**: Add tests for the new issue-association and label-checking logic in the Python workflows.

### Open questions

1. **Should the no-op comment from the initial failed attempt be cleaned up when the `plan-approved` retrigger succeeds?** The initial no-op comment ("I did not start implementation because…") may be confusing if it remains after a successful implementation run. However, it also serves as an audit trail. Recommend leaving it for now and cleaning up in a follow-up if users find it confusing.
