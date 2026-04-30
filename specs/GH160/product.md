# Issue #160: Error states never surface in the progress comment

## Product Spec

### Summary

When a workflow run fails — during agent invocation, transport polling, or any prerequisite step — the progress comment on the issue remains stuck in its initial "Oz is starting…" state indefinitely. The error is never surfaced to the user, and stale transport comments are not cleaned up. This change adds visible error reporting to the progress comment and ensures transport comment cleanup on failure.

### Problem

Every workflow (triage, spec creation, implementation, PR review, PR comment response, triaged issue comment response) posts a progress comment via `WorkflowProgressComment` when it starts. If the workflow fails for any reason — agent failure, transport timeout, GitHub API error, configuration error — the progress comment is never updated. The user sees "Oz is starting to work on…" indefinitely with no indication that something went wrong.

Additionally, when the agent posts a temporary transport comment (e.g. `<!-- oz-workflow-transport ... -->`) before the failure, that comment is left on the issue or PR because the cleanup step is never reached.

This creates a confusing experience: users wait for a response that will never come, and stale bot comments pollute the issue timeline.

### Goals

- When a workflow fails, update the progress comment to indicate that an unexpected error occurred.
- Include a link to the failed GitHub Actions workflow run in the error message so maintainers can debug.
- Clean up any stale transport comments on the issue/PR when a workflow fails.
- Apply this error handling consistently across all six workflow workflows.

### Non-goals

- Retrying failed workflows automatically.
- Changing the happy-path behavior of any workflow.
- Adding detailed error diagnostics (stack traces, internal error messages) to the progress comment. The comment should be user-facing and concise.
- Changing the `WorkflowProgressComment` behavior for non-error paths.
- Changing the agent invocation or transport polling logic itself (e.g. timeout values, retry policies).

### Figma / design references

Figma: none provided. This is a backend/workflow change with no UI beyond GitHub issue comments.

### User experience

#### Error state in the progress comment

When a workflow fails after the progress comment has been posted, the comment is updated to an error state.

When the workflow run URL can be resolved, the error includes a link to the GitHub Actions run:

> @{requester}
>
> Oz ran into an unexpected error while working on this. You can view the [workflow run]({workflow_run_url}) for more details.

When the workflow run URL cannot be resolved (e.g. missing environment variables), the link is dropped entirely:

> @{requester}
>
> Oz ran into an unexpected error while working on this.

If a session link was recorded before the failure, it is preserved regardless of whether the workflow run link is present:

> @{requester}
>
> Oz ran into an unexpected error while working on this. You can view the [workflow run]({workflow_run_url}) for more details.
>
> View the Oz conversation: {session_link}

The workflow run URL `{workflow_run_url}` is resolved deterministically from the standard GitHub Actions environment variables: `$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID`. If `GITHUB_REPOSITORY` or `GITHUB_RUN_ID` is absent, the link is omitted rather than producing an empty or broken link.

#### Behavior rules

1. **Error replaces existing content but preserves the session link.** The error message replaces the status text in the progress comment (e.g. the "starting" or "in progress" message). The metadata marker is preserved. If a session link was recorded before the failure, it is kept in the updated comment because it is a useful debugging tool.

2. **Workflow run link is included when available.** The link to the GitHub Actions run is present in the error message when the URL can be resolved from the standard GitHub Actions environment variables (`GITHUB_SERVER_URL`, `GITHUB_REPOSITORY`, `GITHUB_RUN_ID`). When the URL cannot be constructed (e.g. missing environment variables), the link is dropped entirely rather than producing an empty or broken link. The error message still indicates that an unexpected error occurred.

3. **Transport comment cleanup on error.** If the workflow used transport comments, any stale `<!-- oz-workflow-transport ... -->` comments on the issue/PR are deleted as part of error handling. This prevents orphaned bot comments from cluttering the timeline.

4. **All workflows are covered.** The following scripts all get error handling:
   - `triage_new_issues.py` — wraps the per-issue `process_issue()` call
   - `create_spec_from_issue.py` — wraps the agent invocation and post-processing
   - `create_implementation_from_issue.py` — wraps the agent invocation and post-processing
   - `review_pr.py` — wraps the agent invocation and transport polling
   - `respond_to_pr_comment.py` — wraps the agent invocation and post-processing
   - `respond_to_triaged_issue_comment.py` — wraps the agent invocation and transport polling

5. **Errors before progress comment creation.** If the workflow fails before the progress comment is created (e.g. during configuration or GitHub API setup), no error comment is posted since there is no progress comment to update. The GitHub Actions run will still show the failure in its logs.

6. **The error method is best-effort.** If the GitHub API call to update the progress comment itself fails (e.g. rate limit, network error), the error is silently swallowed. The workflow run logs remain the fallback for debugging.

7. **Transport cleanup is best-effort.** If deleting stale transport comments fails, the error is silently swallowed. This avoids masking the original error.

#### Triage workflow specifics

The triage workflow (`triage_new_issues.py`) already has a try/except around `process_issue()`, but it only emits a GitHub Actions warning — it does not update the progress comment. After this change, the catch block also calls the error method on the progress comment.

Since the progress comment is created inside `process_issue()`, the error handling must also be inside `process_issue()` (wrapping the agent invocation and subsequent steps) rather than only in the outer loop.

### Success criteria

1. When any workflow fails after posting a progress comment, the comment is updated to show the error message with a workflow run link.
2. The error message uses `replace_body()` semantics — it replaces the previous status content, preserving the metadata marker, `@requester` mention, and any previously recorded session link.
3. The workflow run link in the error message is correct and clickable, pointing to the specific GitHub Actions run.
4. Stale transport comments are cleaned up when a workflow fails after the agent has posted them.
5. The happy-path behavior of all workflows is unchanged — no regressions in successful runs.
6. The error handling does not mask or swallow the original exception — the workflow still fails in GitHub Actions so the run is marked as failed.
7. All six workflow scripts have consistent error handling that follows the same pattern.

### Validation

- **Unit tests**: Add tests for the new `report_error()` method on `WorkflowProgressComment`, verifying it updates the comment body with the error message and workflow run link.
- **Unit tests**: Add tests verifying that transport comment cleanup is called during error handling.
- **Integration-level review**: Inspect each workflow script to confirm the try/except wraps the correct scope (after progress comment creation, around agent invocation and post-processing).
- **Manual validation**: Trigger a workflow failure (e.g. by setting an invalid environment variable) and confirm that the progress comment is updated with the error message and the workflow run link is correct.
- **Regression**: Confirm that successful workflow runs produce the same progress comment behavior as before.

### Resolved questions

1. **Should the error message include the Oz session link if one was recorded before the failure?** Yes — the session link is preserved in the error comment if it was already recorded. It is a helpful debugging tool for maintainers.
2. **Should there be a distinct error message for transport timeouts vs. agent failures vs. other errors?** No — use a single generic error message for all failure types to start. The workflow run link provides details. This can be revisited in the future if more granular messaging proves useful.
