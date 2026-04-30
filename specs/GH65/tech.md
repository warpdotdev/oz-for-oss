# Plan: Support mentioning @oz-agent in PR comments to trigger changes

Issue: #65

## Goal
Allow mentioning `@oz-agent` in a PR comment (either a top-level issue comment or a review comment thread) to trigger oz-agent to make changes on the PR's branch.

## Current state
- Existing issue-scoped workflows (`create_plan_from_issue.py`, `create_implementation_from_issue.py`) demonstrate the end-to-end pattern: parse event → gather context → build prompt → `run_agent()` → track progress via `WorkflowProgressComment` → push changes / create PR.
- `review_pr.py` is the closest PR-scoped workflow; it fetches PR metadata, builds a prompt, runs oz-agent, and posts results.
- `resolve_review_context.py` + `pr-hooks.yml` show how an `issue_comment` event on a PR is resolved and forwarded to a reusable workflow.
- `org_member_comments_text()` in `helpers.py` already filters comments to MEMBER/OWNER association.
- `GitHubClient` has no method for listing PR review comments (`/repos/{owner}/{repo}/pulls/{pull_number}/comments`), which are distinct from issue comments.
- GitHub fires two different events:
  - `issue_comment` for top-level PR comments (these are issue-level comments on a PR).
  - `pull_request_review_comment` for code-level review comments in a PR diff.

## Proposed changes

### 1. New GitHub Actions workflow — `.github/workflows/respond-to-pr-comment.yml`

Triggers:
- `issue_comment` (types: `[created]`) — for top-level PR comments mentioning `@oz-agent`.
- `pull_request_review_comment` (types: `[created]`) — for review-thread comments mentioning `@oz-agent`.

Guard conditions (in the `if:` expression):
- The comment body must contain `@oz-agent`.
- `author_association` must be `MEMBER` or `OWNER`.
- For `issue_comment`, the issue must have `.pull_request` (i.e., it's a PR, not a plain issue).
- Exclude bot authors (`github-actions[bot]`).

Concurrency group keyed on the PR number to avoid parallel runs for the same PR.

Reuse the same steps pattern as other workflows: create app token → checkout → GCP auth → gcloud → Python setup → pip install → run script.

Pass env vars: `GH_TOKEN`, `WARP_API_KEY`, standard `WARP_AGENT_*` vars, plus `WARP_AGENT_IMPLEMENTATION_ENVIRONMENT_ID` / `WARP_AGENT_ENVIRONMENT_ID`.

Permissions: `contents: write`, `id-token: write`, `issues: write`, `pull-requests: write` (matching existing workflow patterns).

### 2. New Python script — `src/respond_to_pr_comment.py`

Follows the pattern of `create_implementation_from_issue.py`.

#### Context resolution logic

Determine the trigger type from `GITHUB_EVENT_NAME`:

**Case A — `pull_request_review_comment` (review thread mention):**
1. Extract the review comment from `event["comment"]` and the PR number from `event["pull_request"]["number"]`.
2. Fetch all review comments for the PR via the new `list_pull_review_comments` API method.
3. Identify the thread by following the `in_reply_to_id` chain: collect all comments sharing the same root `id` (comments whose `in_reply_to_id` equals the root, or whose `id` is the root).
4. Filter the thread to MEMBER/OWNER via `author_association`.
5. Format thread comments as context text similar to `org_member_comments_text()`.

**Case B — `issue_comment` (top-level PR comment mention):**
1. Extract PR number from `event["issue"]["number"]`.
2. The comment that triggered the event is the "instruction" comment.
3. Gather context from all issue comments (via `list_issue_comments`) and all review comments (via `list_pull_review_comments`), both filtered to MEMBER/OWNER.
4. Format all of these as context text.

#### Agent invocation
1. Fetch the PR object to get `head.ref` (the branch to push to) and `base.ref`.
2. React to the triggering comment with 👀 to acknowledge.
3. Create a `WorkflowProgressComment` on the PR number with `workflow="respond-to-pr-comment"`.
4. Build a prompt including:
   - PR title, body, base/head branches.
   - The triggering comment text.
   - The gathered context (thread-specific or all-threads).
   - Instruction to use the `implement-issue` skill to make changes on the PR's head branch.
5. Call `build_agent_config()` and `run_agent()` with `skill_name="implement-issue"`.
6. After completion, check if the branch was updated (via `branch_updated_since`).
7. Update the progress comment accordingly.

### 3. `GitHubClient` additions — `src/oz/github_api.py`

Add two new methods:

```python
def list_pull_review_comments(self, owner: str, repo: str, pull_number: int) -> list[dict[str, Any]]:
    return self.paginate(f"/repos/{owner}/{repo}/pulls/{pull_number}/comments")

def create_reaction_for_pull_request_review_comment(
    self, owner: str, repo: str, comment_id: int, content: str
) -> dict[str, Any]:
    return self.request(
        "POST",
        f"/repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions",
        json_body={"content": content},
    )
```

### 4. New helper — `src/oz/helpers.py`

Add a helper function for formatting review comment threads:

```python
def review_thread_comments_text(all_review_comments: list[dict], trigger_comment_id: int) -> str:
```

This finds the thread containing `trigger_comment_id`, filters to MEMBER/OWNER, and formats them. Thread identification uses `in_reply_to_id` to group comments into the same thread.

Add a broader helper for formatting all review comments for the top-level case:

```python
def all_review_comments_text(review_comments: list[dict]) -> str:
```

Filters to MEMBER/OWNER and formats all review comments grouped by file path and thread.

## File change summary

- **New:** `.github/workflows/respond-to-pr-comment.yml`
- **New:** `src/respond_to_pr_comment.py`
- **Modified:** `src/oz/github_api.py` — add `list_pull_review_comments`, `create_reaction_for_pull_request_review_comment`
- **Modified:** `src/oz/helpers.py` — add thread-formatting helpers

## Risks and open questions

1. **Overlap with `pr-hooks.yml`**: The `issue_comment` trigger for this new workflow overlaps with `pr-hooks.yml` which also listens to `issue_comment`. The new workflow's `if:` guard checks for `@oz-agent` in the body (not `/oz-review`), so they should not conflict. However, care is needed to ensure the guards are mutually exclusive.
2. **Review comment thread identification**: GitHub's review comment API uses `in_reply_to_id` for threading. A comment that starts a thread has no `in_reply_to_id`, and all replies point to the first comment's `id`. This is straightforward to implement but should be tested.
3. **Large context**: When `@oz-agent` is mentioned in a top-level comment, pulling all review threads could produce a very large context. Consider truncation or summarization if needed in practice.
4. **Associated issue resolution**: The script should resolve any linked issue to pull in plan context, reusing `resolve_plan_context_for_pr()` from `helpers.py`. This is demonstrated by `review_pr.py` and should be incorporated into the prompt-building step of Section 2.
