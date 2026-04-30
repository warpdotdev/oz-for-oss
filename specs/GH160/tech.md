# Issue #160: Error states never surface in the progress comment

## Tech Spec

### Problem

When any workflow script fails after posting a progress comment via `WorkflowProgressComment`, the comment is never updated to reflect the failure. Users see the initial "Oz is starting…" message indefinitely. Additionally, temporary transport comments (`<!-- oz-workflow-transport ... -->`) left by the agent before the failure are never cleaned up.

The product spec requires: (1) a `report_error()` method on `WorkflowProgressComment` that updates the comment with an error message and a link to the failed GitHub Actions run, (2) a transport comment cleanup utility, and (3) try/except wrappers in all six workflow scripts that call both on failure.

### Relevant code

- `.github/scripts/oz/helpers.py (309-484)` — `WorkflowProgressComment` class with `start()`, `complete()`, `replace_body()`, `cleanup()`, and `_append_sections()`.
- `.github/scripts/oz/helpers.py (345-379)` — `replace_body()` method, which the new `report_error()` will reuse internally.
- `.github/scripts/oz/env.py (17-19)` — `optional_env()` used to read GitHub Actions environment variables.
- `.github/scripts/oz/transport.py (14)` — `TRANSPORT_PATTERN` regex for identifying transport comments.
- `.github/scripts/oz/transport.py (62-95)` — `poll_for_transport_payload()` that raises `RuntimeError` on timeout.
- `.github/scripts/oz/oz_client.py (139-181)` — `run_agent()` that raises `RuntimeError` on non-SUCCEEDED states or timeout.
- `.github/scripts/triage_new_issues.py (118-139)` — existing try/except in `main()` that catches `process_issue()` failures but only emits a warning.
- `.github/scripts/triage_new_issues.py (169-349)` — `process_issue()` where the progress comment is created, the agent is run, and the triage result is applied.
- `.github/scripts/create_spec_from_issue.py (30-149)` — `main()` with no error handling around agent invocation.
- `.github/scripts/create_implementation_from_issue.py (30-200)` — `main()` with no error handling around agent invocation.
- `.github/scripts/review_pr.py (19-157)` — `main()` with no error handling around agent invocation or transport polling.
- `.github/scripts/respond_to_pr_comment.py (115-235)` — `_run_implementation()` with no error handling around agent invocation.
- `.github/scripts/respond_to_triaged_issue_comment.py (43-159)` — `main()` with no error handling around agent invocation or transport polling.
- `.github/scripts/tests/test_comment_updates.py` — existing tests for `WorkflowProgressComment` using `FakeGitHubClient`.

### Current state

`WorkflowProgressComment` provides `start()`, `record_session_link()`, `complete()`, `replace_body()`, and `cleanup()`. There is no method to report an error. The `replace_body()` method provides the right primitive — it replaces the full comment body while preserving the metadata marker and `@requester` mention.

Each workflow script follows roughly the same pattern:
1. Create `WorkflowProgressComment` and call `progress.start(...)`.
2. Call `run_agent(...)` (and optionally `poll_for_transport_payload(...)`).
3. Process the result and call `progress.complete(...)`.

If step 2 or 3 raises an exception, the progress comment is never updated. Only `triage_new_issues.py` has a try/except, but it only emits a `warning()` annotation — it does not touch the progress comment.

Three of the six workflows use transport comments (`triage_new_issues.py`, `review_pr.py`, `respond_to_triaged_issue_comment.py`). When these workflows fail between the agent posting a transport comment and the workflow deleting it, the transport comment is orphaned.

The GitHub Actions environment variables needed for the workflow run link (`GITHUB_SERVER_URL`, `GITHUB_REPOSITORY`, `GITHUB_RUN_ID`) are standard and always available in GitHub Actions runners. `GITHUB_REPOSITORY` is already used by `env.py:repo_slug()`. `GITHUB_SERVER_URL` defaults to `https://github.com`. `GITHUB_RUN_ID` is not currently referenced anywhere in the codebase.

### Proposed changes

#### 1. Add `report_error()` to `WorkflowProgressComment` in `helpers.py`

Add a new method that builds the error message with a workflow run link and preserves the session link if one was recorded:

```python
def report_error(self) -> None:
    """Update the progress comment to indicate a workflow failure."""
    try:
        run_url = _workflow_run_url()
        if run_url:
            message = (
                "Oz ran into an unexpected error while working on this. "
                f"You can view the [workflow run]({run_url}) for more details."
            )
        else:
            message = "Oz ran into an unexpected error while working on this."
        sections = [message]
        if self.session_link:
            sections.append(_format_progress_link_section(self.session_link))
        self.replace_body("\n\n".join(sections))
    except Exception:
        pass
```

The method preserves the session link because it is a useful debugging tool for maintainers. `self.session_link` is set by `record_session_link()` (see change below).

The method is wrapped in a bare try/except so it never raises — it is always called from an error path and must not mask the original exception.

Add a module-level helper to construct the workflow run URL:

```python
def _workflow_run_url() -> str:
    """Build the GitHub Actions workflow run URL from environment variables."""
    server_url = optional_env("GITHUB_SERVER_URL") or "https://github.com"
    repository = optional_env("GITHUB_REPOSITORY")
    run_id = optional_env("GITHUB_RUN_ID")
    if not repository or not run_id:
        return ""
    return f"{server_url}/{repository}/actions/runs/{run_id}"
```

This requires importing `optional_env` from `oz.env` in `helpers.py`. The `env` module is already a dependency of the workflow scripts but not currently imported by `helpers.py`. This is a new import — it creates a dependency from `helpers.py` → `env.py`, which is acceptable since `env.py` has no dependencies on `helpers.py` (no circular import risk).

If the URL cannot be constructed (e.g. missing env vars in a test environment), `_workflow_run_url()` returns an empty string. When the URL is empty, `report_error()` omits the workflow run link entirely and uses a plain error message without producing an empty or broken link.

Additionally, update `record_session_link()` to store the link on `self.session_link` so that `report_error()` can include it in the error comment:

```python
def record_session_link(self, session_link: str) -> None:
    if not session_link.strip():
        return
    self.session_link = session_link.strip()
    self._append_sections([_format_progress_link_section(session_link)])
```

The `self.session_link` field already exists on the class (initialized to `""` in `__init__`), but is not currently set by `record_session_link()`. This change stores the link so `report_error()` can re-include it in the error state.

#### 2. Add `cleanup_transport_comments()` to `transport.py`

Add a function that finds and deletes all transport comments on an issue/PR:

```python
def cleanup_transport_comments(
    github: Repository | Any,
    owner: str,
    repo: str,
    issue_number: int,
) -> None:
    """Delete any oz-workflow-transport comments on the given issue/PR. Best-effort."""
    try:
        if hasattr(github, "get_issue"):
            comments = list(github.get_issue(issue_number).get_comments())
        else:
            comments = github.list_issue_comments(owner, repo, issue_number)
        for comment in comments:
            body = (
                str(comment.get("body") or "")
                if isinstance(comment, dict)
                else str(getattr(comment, "body", "") or "")
            )
            if TRANSPORT_PATTERN.search(body):
                try:
                    if hasattr(comment, "delete"):
                        comment.delete()
                    else:
                        comment_id = (
                            comment.get("id")
                            if isinstance(comment, dict)
                            else getattr(comment, "id", None)
                        )
                        if comment_id is not None:
                            # Use the helpers module's delete function
                            from .helpers import _delete_issue_comment
                            _delete_issue_comment(github, owner, repo, issue_number, int(comment_id))
                except Exception:
                    pass
    except Exception:
        pass
```

This function is entirely best-effort — every operation is wrapped in try/except to avoid masking the original error. It uses the existing `TRANSPORT_PATTERN` regex to identify transport comments.

#### 3. Add try/except to each workflow script

Each script gets the same pattern: wrap the agent invocation and all subsequent processing in a try/except that calls `progress.report_error()` and transport cleanup, then re-raises.

**Pattern for workflows that use transport comments** (`triage_new_issues.py`, `review_pr.py`, `respond_to_triaged_issue_comment.py`):

```python
try:
    run = run_agent(...)
    payload, transport_comment_id = poll_for_transport_payload(...)
    # ... process result ...
    progress.complete(...)
except Exception:
    progress.report_error()
    cleanup_transport_comments(github, owner, repo, issue_number)
    raise
```

**Pattern for workflows that do not use transport comments** (`create_spec_from_issue.py`, `create_implementation_from_issue.py`, `respond_to_pr_comment.py`):

```python
try:
    run = run_agent(...)
    # ... process result ...
    progress.complete(...)
except Exception:
    progress.report_error()
    raise
```

The `raise` at the end ensures the original exception propagates and the GitHub Actions run is marked as failed.

##### Per-script specifics:

**`triage_new_issues.py`**: The try/except goes inside `process_issue()`, wrapping lines 286–348 (from `run_agent(...)` through `progress.replace_body(...)`). The existing outer try/except in `main()` (lines 120–139) stays as-is for catching errors outside `process_issue()`.

**`create_spec_from_issue.py`**: Wrap lines 99–149 (from `run_agent(...)` through `progress.complete(...)`).

**`create_implementation_from_issue.py`**: Wrap lines 147–200 (from `run_agent(...)` through `progress.complete(...)`).

**`review_pr.py`**: Wrap lines 114–157 (from `run_agent(...)` through `progress.complete(...)`). Include `cleanup_transport_comments()` in the except block.

**`respond_to_pr_comment.py`**: Wrap lines 208–235 (from `run_agent(...)` through `progress.complete(...)`).

**`respond_to_triaged_issue_comment.py`**: Wrap lines 133–159 (from `run_agent(...)` through `progress.complete(...)`). Include `cleanup_transport_comments()` in the except block.

### End-to-end flow

**Happy path (unchanged):**
1. Workflow starts → `progress.start("Oz is starting...")` → comment created.
2. Agent runs → `on_poll` callback updates session link.
3. Agent completes → result processed → `progress.complete("...")` → comment updated.

**Error path (new):**
1. Workflow starts → `progress.start("Oz is starting...")` → comment created.
2. Agent runs → (may update session link via `on_poll`).
3. Agent fails / transport times out / post-processing fails → exception raised.
4. Except block catches the exception:
   a. `progress.report_error()` → comment updated with error message and workflow run link.
   b. `cleanup_transport_comments(...)` → any stale transport comments deleted (for workflows that use transport).
   c. `raise` → exception propagates, GitHub Actions run marked as failed.

### Risks and mitigations

**Risk: `report_error()` itself fails (e.g. GitHub API rate limit).**
Mitigation: The method is wrapped in a bare try/except and silently swallows errors. The workflow still fails via the re-raised exception, and the GitHub Actions run logs contain the original error.

**Risk: Transport cleanup deletes a comment that was still needed.**
Mitigation: Transport comments are temporary by design and only relevant during the current workflow run. By the time `cleanup_transport_comments()` runs in the error path, the workflow has already failed and will not attempt to read the transport comment.

**Risk: Circular import between `helpers.py` and `env.py`.**
Mitigation: `env.py` has no imports from `helpers.py` or any other module in `oz` (only `os`, `json`, `pathlib`). Adding `from .env import optional_env` to `helpers.py` is safe.

**Risk: Tests break because `GITHUB_SERVER_URL` / `GITHUB_RUN_ID` are not set in test environments.**
Mitigation: `_workflow_run_url()` uses `optional_env()` and returns an empty string when the variables are absent. `report_error()` can handle an empty URL gracefully (the message still makes sense without the link, or the link can be omitted).

### Testing and validation

**Unit tests for `report_error()`:**
- Add a test in `test_comment_updates.py` using the existing `FakeGitHubClient` pattern.
- Test that after `progress.start(...)` followed by `progress.report_error()`, the comment body contains the error message text.
- Test that the `@requester` mention is preserved.
- Test that the metadata marker is preserved.
- Test that if the progress comment was never created (no `start()` call), `report_error()` creates one.
- Test that if a session link was recorded before the error, it is preserved in the error comment.

**Unit tests for `cleanup_transport_comments()`:**
- Add a test in `test_transport.py` that creates comments with transport markers and verifies they are deleted.
- Test that non-transport comments are left untouched.
- Test that errors during cleanup are silently swallowed.

**Unit test for `_workflow_run_url()`:**
- Test with all env vars set → returns correct URL.
- Test with missing env vars → returns empty string.

**Integration-level validation:**
- Inspect each workflow script to confirm the try/except wraps the correct scope.
- Confirm that the `raise` statement preserves the original exception.

### Follow-ups

- Consider adding structured error categorization (timeout vs. agent failure vs. API error) in a future iteration if the generic message proves insufficient for debugging.
- The `triage_new_issues.py` outer try/except in `main()` (lines 137-139) could also call `progress.report_error()`, but since the progress comment is created inside `process_issue()`, the `progress` variable is not in scope there. A follow-up could restructure this if needed.
