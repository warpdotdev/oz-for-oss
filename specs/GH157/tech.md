# Issue #157: Triage issue agent duplicates comments posted

## Tech Spec

### Problem

The triage workflow in `process_issue()` creates three independent comment streams, each with its own metadata marker and lifecycle. This results in up to three separate comments on a single issue. The product spec requires consolidating all triage output into a single comment that updates in place through three stages, and formatting session links as markdown links.

### Relevant code

- `.github/scripts/triage_new_issues.py:160-328` — `process_issue()` orchestrates the triage flow; calls `WorkflowProgressComment`, `sync_follow_up_comment()`, `sync_duplicate_comment()`, and `progress.complete()` independently.
- `.github/scripts/triage_new_issues.py:307-320` — `sync_follow_up_comment()` and `sync_duplicate_comment()` calls that create separate comments.
- `.github/scripts/triage_new_issues.py:324-327` — `progress.complete()` call that finalizes the progress comment without including follow-up or duplicate content.
- `.github/scripts/triage_new_issues.py:443-463` — `follow_up_comment_metadata()` and `build_follow_up_comment()` construct the standalone follow-up comment.
- `.github/scripts/triage_new_issues.py:538-571` — `duplicate_comment_metadata()` and `build_duplicate_comment()` construct the standalone duplicate comment.
- `.github/scripts/triage_new_issues.py:466-503` — `sync_follow_up_comment()` manages the follow-up comment lifecycle.
- `.github/scripts/triage_new_issues.py:574-591` — `sync_duplicate_comment()` manages the duplicate comment lifecycle.
- `.github/scripts/oz/helpers.py:303-441` — `WorkflowProgressComment` class manages the progress comment lifecycle.
- `.github/scripts/oz/helpers.py:211-215` — `_format_progress_link_section()` formats session links as raw URLs.
- `.github/scripts/oz/helpers.py:205-208` — `_PROGRESS_LINK_PREFIXES` tuple used for deduplicating link sections.
- `.github/scripts/tests/test_triage.py` — existing tests for follow-up, duplicate, and triage result application.

### Current state

`process_issue()` runs three comment operations sequentially after receiving the triage result:

1. **Progress comment** (`WorkflowProgressComment`): Created at start with `progress.start()`, updated when session link becomes available via `record_run_session_link()`, finalized with `progress.complete()`. Uses `issue-status` metadata type. The `complete()` call appends a summary and disclaimer to the existing progress content.

2. **Follow-up comment** (`sync_follow_up_comment()`): Created as a separate comment with `issue-triage-follow-up` metadata type. Contains `@reporter` mention, contextual preamble, numbered questions, and disclaimer. Deletes itself if questions list is empty.

3. **Duplicate comment** (`sync_duplicate_comment()`): Created as a separate comment with `issue-triage-duplicate` metadata type. Contains `@reporter` mention, list of duplicate issues with similarity reasons, and disclaimer.

Session links are formatted by `_format_progress_link_section()` which produces either `"View the Oz conversation: {url}"` or `"Sharing session at: {url}"` depending on whether the URL contains `/conversation/`. These are raw URLs, not markdown links.

The `WorkflowProgressComment` class uses `append_comment_sections()` to update its comment body. This function splits the body on `\n\n`, deduplicates link-prefix sections, and appends new sections. The `complete()` method calls `_append_sections()` which uses the same logic.

### Proposed changes

#### 1. Add `_format_triage_session_link()` in `helpers.py`

Add a new triage-specific session link formatter that uses markdown link syntax. The existing `_format_progress_link_section()` is preserved unchanged to avoid breaking non-triage workflows that use `record_session_link()` and the `_PROGRESS_LINK_PREFIXES` dedup logic in `append_comment_sections()`.

```python
def _format_triage_session_link(session_link: str) -> str:
    """Format a session link as a markdown link for the triage workflow."""
    normalized_link = session_link.strip()
    return f"[the triage session on Warp]({normalized_link})"
```

This returns only the markdown link fragment, not a full sentence. The sentence context is provided by the callers in the triage progress comment stages.

#### 2. Change progress comment stages in `WorkflowProgressComment`

Currently `WorkflowProgressComment` accumulates sections via `_append_sections()`. For the triage workflow, the three stages require replacing content rather than appending. The cleanest approach: add a method to `WorkflowProgressComment` that **replaces** the entire comment content (not appending) while preserving the metadata marker.

Add a `replace_body(content: str)` method:

```python
def replace_body(self, content: str) -> None:
    """Replace the full comment body, preserving the metadata marker."""
    requester = resolve_progress_requester_login(...)
    sections = []
    if requester:
        sections.append(f"@{requester}")
    sections.append(content)
    body = build_comment_body("\n\n".join(sections), self.metadata)
    existing = self._get_or_find_existing_comment()
    if existing is None:
        created = _create_issue_comment(...)
        self.comment_id = int(_field(created, "id"))
        return
    _update_issue_comment(..., body)
    self.comment_id = int(_field(existing, "id"))
```

This approach preserves backward compatibility: `start()`, `record_session_link()`, and `complete()` continue to use `_append_sections()` for non-triage workflows. The triage workflow will use `replace_body()` for its stage transitions.

#### 3. Refactor `process_issue()` in `triage_new_issues.py`

Replace the three independent comment operations with staged updates to the single progress comment:

**Stage 1 (before agent run):**
```python
progress.start("Oz is starting to work on triaging this issue.")
```
This stays the same.

**Stage 2 (session link available):**
The `record_run_session_link()` callback updates the comment. Change the session link recording to produce the Stage 2 message:

```python
def record_run_session_link(progress: WorkflowProgressComment, run: object) -> None:
    session_link = getattr(run, "session_link", None) or ""
    if not session_link.strip():
        return
    link = _format_progress_link_section(session_link)
    progress.replace_body(
        f"Oz is triaging this issue. You can follow {link}."
    )
```

However, `record_run_session_link()` is a shared helper used by all workflows, not just triage. To avoid changing non-triage workflows, introduce a triage-specific callback instead:

```python
on_poll=lambda current_run: _record_triage_session_link(progress, current_run),
```

Where `_record_triage_session_link()` is a local function in `triage_new_issues.py` that calls `progress.replace_body()` with the Stage 2 message. The generic `record_run_session_link()` remains unchanged for other workflows.

**Stage 3 (after triage result):**

Build the complete Stage 3 body and call `progress.replace_body()`:

```python
summary = _lowercase_first(str(result.get("summary") or "triage completed").strip())
session_link_text = _format_progress_link_section(session_link) if session_link else ""

parts = []
if session_link_text:
    parts.append(
        f"Oz has completed the triage of this issue. "
        f"You can view {session_link_text}.\n\n"
        f"The triage concluded that {summary}."
    )
else:
    parts.append(
        f"Oz has completed the triage of this issue. "
        f"The triage concluded that {summary}."
    )
```

`_lowercase_first()` lowercases the first character of the summary so it reads naturally mid-sentence (e.g. "The triage concluded that the issue appears..." instead of "The triage concluded that The issue appears...").

follow_up_questions = extract_follow_up_questions(result)
duplicates = extract_duplicate_of(result, current_issue_number=issue_number)

# Follow-up questions and duplicates are mutually exclusive.
# If duplicates are found, suppress follow-up questions.
if duplicates:
    parts.append(build_duplicate_section(issue, duplicates))
elif follow_up_questions:
    parts.append(build_follow_up_section(issue, follow_up_questions))

parts.append(TRIAGE_DISCLAIMER)
progress.replace_body("\n\n".join(parts))
```

Remove the calls to `sync_follow_up_comment()` and `sync_duplicate_comment()` from `process_issue()`.

#### 4. Add `build_follow_up_section()` and `build_duplicate_section()`

These are new functions that produce the section content (without metadata markers or standalone comment wrappers) for embedding in the progress comment:

```python
def build_follow_up_section(issue: Any, questions: list[str]) -> str:
    reporter_login = _login(_field(issue, "user")).strip()
    lines = ["### Follow-up questions", ""]
    if reporter_login:
        lines.append(f"@{reporter_login}")
        lines.append("")
    lines.append(
        "Thanks for the report. I'm missing a few issue-specific details "
        "before I can narrow this down confidently:"
    )
    lines.append("")
    lines.extend(f"{i}. {q}" for i, q in enumerate(questions, start=1))
    lines.append("")
    lines.append(
        "Reply in-thread with those details and the triage workflow will "
        "automatically re-evaluate the issue and update the diagnosis, "
        "labels, and next steps."
    )
    return "\n".join(lines)
```

```python
def build_duplicate_section(issue: Any, duplicates: list[dict[str, Any]]) -> str:
    lines = ["### Potential duplicates", ""]
    lines.append("This issue appears likely to overlap with the following existing issues:")
    lines.append("")
    for dup in duplicates:
        num = dup["issue_number"]
        title = dup.get("title") or ""
        reason = dup.get("similarity_reason") or ""
        line = f"- #{num}"
        if title:
            line += f" — {title}"
        lines.append(line)
        if reason:
            lines.append(f"  Why it looks similar: {reason}")
    lines.append("")
    lines.append(
        "If this report is meaningfully different, please comment with the "
        "additional context or distinguishing behavior so a maintainer can "
        "review it. Otherwise, a maintainer may close it as a duplicate after review."
    )
    return "\n".join(lines)
```

#### 5. Create a new comment on re-triage

When an explicit re-triage is invoked, the `WorkflowProgressComment` should always create a **new** comment rather than finding and editing the existing one. To achieve this, generate a fresh `run_id` for each `WorkflowProgressComment` instance (already the case) and ensure that `_get_or_find_existing_comment()` only matches comments with the exact same `run_id` metadata, not any comment from the same workflow. Since the metadata already includes `run_id`, a new run will naturally create a new comment. The previous run's comment remains in the issue timeline for history.

#### 6. Clean up legacy standalone comments on re-triage

Add a cleanup step at the beginning of `process_issue()` (after creating the `WorkflowProgressComment`) that finds and deletes any orphaned standalone follow-up or duplicate comments from prior runs:

```python
_cleanup_legacy_triage_comments(github, owner, repo, issue)
```

This function searches for comments containing `issue-triage-follow-up` or `issue-triage-duplicate` metadata markers and deletes them. This handles the migration from old multi-comment behavior to the new consolidated comment.

#### 7. Deprecate `sync_follow_up_comment()` and `sync_duplicate_comment()`

These functions remain in the codebase for now (they are tested and may be useful for other workflows), but are no longer called from `process_issue()`. Add a comment marking them as deprecated. If no other callers exist, they can be removed in a follow-up.

Similarly, `build_follow_up_comment()` and `build_duplicate_comment()` (which produce standalone comment bodies with metadata) are no longer needed by the triage workflow but can be retained for backward compatibility or removed in a follow-up.

#### 8. Track the session link for Stage 3

The session link must be available when building the Stage 3 body. Currently `record_run_session_link()` is called during polling and the link is consumed immediately. To make the link available at Stage 3:

Store the last known session link on the `WorkflowProgressComment` instance. Add a `session_link` attribute:

```python
class WorkflowProgressComment:
    def __init__(self, ...):
        ...
        self.session_link: str = ""
```

The triage-specific poll callback updates both the comment and stores the link:

```python
def _record_triage_session_link(progress: WorkflowProgressComment, run: object) -> None:
    session_link = getattr(run, "session_link", None) or ""
    if not session_link.strip():
        return
    progress.session_link = session_link.strip()
    link = _format_progress_link_section(progress.session_link)
    progress.replace_body(
        f"Oz is triaging this issue. You can follow {link}."
    )
```

Then at Stage 3, `progress.session_link` is available.

### End-to-end flow

1. `process_issue()` creates `WorkflowProgressComment` (with a fresh `run_id`) and calls `progress.start("Oz is starting to work on triaging this issue.")`. A **new** comment is created (Stage 1), even if a previous triage comment exists on the issue.
2. `_cleanup_legacy_triage_comments()` removes any orphaned standalone follow-up/duplicate comments from prior runs.
3. `run_agent()` starts with `on_poll=lambda run: _record_triage_session_link(progress, run)`. When session link becomes available, the comment is replaced with Stage 2 content.
4. Agent completes. `poll_for_transport_payload()` retrieves the triage result.
5. Transport comment is deleted.
6. `apply_triage_result()` applies labels and updates the issue body (unchanged).
7. `process_issue()` builds the Stage 3 body by combining the completion message, session link, and either the follow-up questions section or the duplicate section (mutually exclusive), plus the disclaimer.
8. `progress.replace_body()` updates the comment to Stage 3.
9. `append_summary()` logs the triage result.

### Risks and mitigations

- **Non-triage workflows affected**: The `replace_body()` method is new and only called from triage code. Existing `start()`/`complete()`/`record_session_link()` behavior is unchanged, so spec-creation and implementation workflows are not affected.
- **Race condition on comment update**: If the poll callback fires while Stage 3 is being written, the comment could be overwritten. This is already the case today and is mitigated by the sequential nature of `poll_for_transport_payload()` completing before Stage 3 runs.
- **Legacy comment cleanup**: The cleanup function should be defensive — catch exceptions on delete and continue. If a legacy comment can't be deleted, it's not critical.
- **Session link not available**: If the session link is never populated (e.g., agent errors early), Stage 3 omits the session link gracefully.
- **Mutual exclusivity enforcement**: The workflow code enforces mutual exclusivity of follow-up questions and duplicates at the comment-building layer. If the triage agent returns both (violating the skill constraint), duplicates take precedence and follow-up questions are suppressed.
- **Re-triage comment accumulation**: Each re-triage creates a new comment rather than editing in place. Over many re-triages, this could accumulate comments. This is acceptable for auditability and is the same pattern used by other CI/bot workflows. If it becomes noisy, a future change can add optional cleanup of previous triage comments.

### Testing and validation

#### Unit tests to update

1. **`SyncFollowUpCommentTest`**: Tests should be updated or supplemented to verify that follow-up content is embedded in the progress comment via `build_follow_up_section()` rather than created as a standalone comment. The existing `sync_follow_up_comment()` tests can remain for backward compatibility testing of the deprecated function.

2. **`SyncDuplicateCommentTest`**: Same approach — add tests for `build_duplicate_section()` and verify it produces the expected section content.

3. **`BuildFollowUpSectionTest` (new)**: Test that `build_follow_up_section()` produces correctly formatted section content with reporter mention, numbered questions, and context text.

4. **`BuildDuplicateSectionTest` (new)**: Test that `build_duplicate_section()` produces correctly formatted section content with issue links, titles, and similarity reasons.

5. **`WorkflowProgressComment.replace_body` (new)**: Test that `replace_body()` replaces the full comment content while preserving metadata.

6. **`_format_progress_link_section` update**: Test that session links are formatted as markdown `[Warp](url)`.

7. **`_cleanup_legacy_triage_comments` (new)**: Test that orphaned follow-up and duplicate comments are found and deleted.

8. **Mutual exclusivity enforcement test** (new): Verify that when both follow-up questions and duplicates are present in the triage result, only the duplicate section appears in the final comment.

9. **Integration test for `process_issue()` comment count**: Mock the full triage flow and assert exactly one new comment is created per triage run, containing progress and either follow-up or duplicate sections (never both).

#### Manual validation

- Trigger triage on a test issue and verify the GitHub issue shows one comment progressing through three stages.
- Verify session links are clickable markdown links.
- Verify re-triage updates the existing comment and cleans up legacy standalone comments.

### Follow-ups

- Remove `sync_follow_up_comment()`, `sync_duplicate_comment()`, `build_follow_up_comment()`, and `build_duplicate_comment()` once confirmed no other callers depend on them.
- Consider whether `_PROGRESS_LINK_PREFIXES` and the `append_comment_sections()` link-dedup logic should be simplified now that session links are embedded in stage messages rather than appended as separate sections.
- Consider applying the markdown link format to non-triage workflows' session links for consistency (separate issue).
- Consider adding optional cleanup of previous triage progress comments on re-triage if comment accumulation becomes noisy.
