---
name: github
description: "Mechanics for working with GitHub PRs with the gh CLI: branches and commits, creating and updating draft PRs, reading a PR and all of its feedback surfaces, posting a review with inline comments, checking CI and mergeability, and managing reviewers. Use for any branch, commit, or PR operation."
---

# github

How to use GitHub. This skill covers the mechanics of the surface, not what to implement or how to judge a change.

## Tools

- Use the `gh` CLI, non-interactively. Pass `--repo <owner>/<repo>` on every command.
- Set `GH_PAGER=cat` on every `gh` invocation to disable paging. Do not pass `--no-pager` to `gh`; the GitHub CLI does not support it as a global option.
- Use `git --no-pager` for local operations; `--no-pager` is valid for `git`.
- Never merge a PR.

## Branches and commits

- Branch off the repository's base branch. Confirm the base branch from the repository's conventions; do not assume `main`. Capture it once into a variable (e.g. `BASE_BRANCH=develop`) when you check out, and reuse that same variable everywhere a base branch is named - including the PR's `--base` - instead of typing a branch name again.
- Name factory branches `factory/<short-slug>`.
- The git identity and GitHub credentials for this run are already configured. Use them as-is; never set or override them, whether through `git config user.*`, `git -c user.*`, `--author`, or the `GIT_AUTHOR_*`/`GIT_COMMITTER_*` environment variables.
- A factory's or agent's display name belongs in labels, comments, and footers - never in a commit's author or committer field.

## Creating a PR

```bash
GH_PAGER=cat gh pr create --draft --repo <owner>/<repo> --head <branch> --base "$BASE_BRANCH" \
  --title "<title>" --body-file /tmp/pr_body.md
```

- Write the body to a file first. Do not inline multi-line bodies in the command.
- Size the body to the change, not the template - a body is as permanent as a comment. The implementation agent's PR description section sets the proportionality rule; this skill only carries the mechanics.
- Verify the creation: `GH_PAGER=cat gh pr view <pr> --repo <owner>/<repo> --json headRefName,baseRefName,changedFiles`. A wrong head branch, a `baseRefName` that does not match `$BASE_BRANCH`, or 0 changed files means the PR is wrong. Close it and create it again correctly.
- Promote when ready: `GH_PAGER=cat gh pr ready <pr> --repo <owner>/<repo>`.
- Update title or body: `GH_PAGER=cat gh pr edit <pr> --repo <owner>/<repo> --title "<title>" --body-file <file>`.

## Reporting the PR as a run output

Call the `report_pr` tool with the PR's HTTPS URL (e.g. `https://github.com/<owner>/<repo>/pull/<number>`) and its head branch as soon as the PR exists in your run — whether you just created it or you adopted an existing one (e.g. picking up review of a PR opened earlier). This is what makes the PR show up as a run output in the UI; without it, nothing is recorded even though the PR itself exists. Adopting a PR is easy to treat as "nothing to report" since you did not open it — report it anyway.

## Labeling a PR or issue

Every PR and issue you open or adopt carries this factory's own label, `factory:<alias>`. Apply it as soon as you have the artifact in hand.

Resolve the alias first. It is the top-level `alias` in the factory's own `factory.yaml`, read from `WARP_SKILL_DIRS` the same way as `<factory name>` under Posting comments below. Resolve it once per run and reuse it:

```bash
skills_root="${WARP_SKILL_DIRS##*,}"
factory_root="${skills_root%/skills}"
factory_alias=$(sed -n 's/^alias:[[:space:]]*//p' "$factory_root/factory.yaml" | head -1 \
  | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")
```

An empty `$factory_alias` means the label is unresolved - warn and leave the artifact unlabeled. Never apply a bare `factory:`.

Canonicalize the parsed value the way the platform derives its own label (`deriveFactoryLabel` in `logic/factory_labels.go`), or a raw value can create a second, mismatched label instead of reusing the one the platform already manages:

```bash
factory_alias=$(printf '%s' "$factory_alias" | tr -s '[:space:][:cntrl:]' ' ' | sed -e 's/^ *//' -e 's/ *$//')
if [ "$(printf '%s' "$factory_alias" | wc -m)" -gt 42 ]; then
  factory_alias="$(printf '%s' "$factory_alias" | cut -c1-41)…"
fi
```

With the alias in hand, run the line that matches the artifact:

```bash
GH_PAGER=cat gh pr edit <pr> --repo <owner>/<repo> --add-label "factory:$factory_alias"
GH_PAGER=cat gh issue edit <issue> --repo <owner>/<repo> --add-label "factory:$factory_alias"
```

If no label exists, create it first then try re-applying:

```bash
GH_PAGER=cat gh label create "factory:$factory_alias" --repo <owner>/<repo>
```

Applying it blocks nothing: on failure, warn and carry on with an unlabeled artifact rather than aborting. Re-applying is free, so apply rather than checking first.

## Reading a PR

```bash
GH_PAGER=cat gh pr view <pr> --repo <owner>/<repo> --json title,body,headRefOid,baseRefName,files,isDraft
GH_PAGER=cat gh pr diff <pr> --repo <owner>/<repo>
GH_PAGER=cat gh pr checkout <pr> --repo <owner>/<repo>
```

## Reading all feedback surfaces

A PR has three comment surfaces. `GH_PAGER=cat gh pr view --comments` shows only one of them. Read all three, each with `--paginate`:

```bash
GH_PAGER=cat gh api repos/<o>/<r>/issues/<n>/comments --paginate   # conversation comments
GH_PAGER=cat gh api repos/<o>/<r>/pulls/<n>/reviews --paginate     # top-level reviews
GH_PAGER=cat gh api repos/<o>/<r>/pulls/<n>/comments --paginate    # inline comments
```

An empty result from one surface does not mean there is no feedback. For thread resolution state (`isResolved`, `isOutdated`), query GraphQL `reviewThreads` with a cursor loop.

## Posting comments

A GitHub comment is permanent and stacks in the PR timeline - be concise. Aim for 4 lines and 300 characters.

### Footer

End every comment - including replies from `scripts/resolve-threads` - with:

```
Responding as <factory name>: [Open session](<session url>) · [View in factory](<factory url>)
```

No link back to the PR - redundant on the surface the reader is already on. Resolve the values once per run and reuse them. Only `<factory name>` is blocking - never post without it. `<session url>` and `<factory url>` degrade instead: drop either that can't be resolved, even both, rather than skipping the comment. Does not apply to the PR description or the managed attribution comment; see "Attribution comment" below.

- To @-mention a person in a comment, reply, or PR description, use the GitHub login provided to you. Never type a handle inferred from a display name, an email, or a chat handle.
- `<factory name>`: the top-level `name` in the factory's own `factory.yaml`, checked out next to the skills. `WARP_SKILL_DIRS` lists `<checkout>/agents/<agent>/skills,<checkout>/skills`, relative to the environment's working directory - resolve from there, or `cd` back to it first:
  ```bash
  skills_root="${WARP_SKILL_DIRS##*,}"
  factory_root="${skills_root%/skills}"
  factory_name=$(sed -n 's/^name:[[:space:]]*//p' "$factory_root/factory.yaml" | head -1 \
    | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")
  ```
  `^name:` matches only the unindented top-level key, never a nested one - YAML block mappings indent every nested key; stripping trailing whitespace and either quote style keeps interior spaces so a multi-word name isn't truncated. Treat an unset `WARP_SKILL_DIRS`, a missing `factory.yaml`, or an empty parsed name as blocking. A live-managed factory has no `WARP_SKILL_DIRS` and loads no file-based skills, so this never runs there.
- `<session url>` needs `$OZ_CLI`, the path or command the runtime exports for the run. Never hardcode the binary name - it varies by channel:
  ```bash
  "$OZ_CLI" run get "$OZ_RUN_ID" --output-format json --jq '.session_link // empty'
  ```
- `<factory url>`: `${WARP_FACTORY_ORIGIN}/${WARP_FACTORY_ID}/activity` - both env vars are exported for every factory run. Opens the factory's activity feed, not the specific task.

### Attribution comment

Post the managed attribution comment (`<!-- warp:pr-artifacts-comment start -->` / `<!-- warp:pr-artifacts-comment end -->`) right after creating the PR; when adopting one that already existed before this run, post it before your first other write to it instead. Check first: a comment already carrying both markers means it is posted - do nothing, and never resolve a duplicate yourself. It is exempt from the Footer above - post its body verbatim, with no footer. Use the run/conversation/thread links your brief supplied, not your own sub-run's, so the reader lands on the factory task - but swap only those `href` values and copy every other byte of the handed-down block, its image markup and both marker pairs included. Never rebuild it from memory. Not posting it blocks nothing - note it in your report and move on.

## Posting a review

Post the body and all inline comments in one API call:

```bash
cat > /tmp/review_body.md << 'EOF'
...
EOF
jq -n '[{"path":"src/f.py","line":42,"side":"RIGHT","body":"..."}]' > /tmp/review_comments.json
HEAD_SHA=$(GH_PAGER=cat gh pr view <pr> --repo <o>/<r> --json headRefOid -q .headRefOid)
jq -n --rawfile body /tmp/review_body.md \
  --slurpfile comments /tmp/review_comments.json \
  --arg event "REQUEST_CHANGES" --arg sha "$HEAD_SHA" \
  '{"commit_id":$sha,"body":$body,"event":$event,"comments":$comments[0]}' | \
  GH_PAGER=cat gh api repos/<o>/<r>/pulls/<pr>/reviews --method POST --input -
```

- `event` is `REQUEST_CHANGES`, `COMMENT`, or `APPROVE`.
- An inline comment must point at a file and line in the PR's diff. GitHub silently drops comments on unchanged lines. Verify each `path` and `line` against the diff first, and move the rest into the body.
- A suggestion block replaces exactly the commented line range. Keep the indentation exact, and do not include lines from outside the range.

## CI and mergeability

```bash
GH_PAGER=cat gh pr checks <pr> --repo <owner>/<repo>
GH_PAGER=cat gh pr view <pr> --repo <owner>/<repo> --json mergeable,mergeStateStatus
```

- `mergeable: MERGEABLE` with `mergeStateStatus` of `CLEAN`, `BLOCKED`, or `HAS_HOOKS` is a healthy state. `BLOCKED` frequently means only that a review or branch protection is outstanding.
- `CONFLICTING`, `DIRTY`, or `BEHIND`: fetch the base branch and merge it into the PR branch. Do not rebase a branch that others share. Resolve only mechanically safe conflicts, push, and check again.
- `UNKNOWN` or null: GitHub is still computing. Retry a few times with short delays. Do not convert an unknown result into a claim of success.
- These commands are point-in-time reads. Do not block on CI: never sleep-and-recheck until the checks finish before reporting or handing off a PR. Report the current state and move on; a CI failure that lands later arrives as its own follow-up.

## Resolving review threads

After a revision addresses a review finding:
1. Reply in that thread with the commit that addresses it (link the commit SHA).
2. Resolve the thread through the GraphQL `resolveReviewThread` mutation, with the thread ID from the `reviewThreads` query.

Use the bundled script for both steps in one call, with `--footer` from Posting comments above:

```bash
scripts/resolve-threads --repo <owner>/<repo> --pr <number> \
  --thread-ids <PRRT_id1,PRRT_id2> --revision <N> --commit-sha <sha> \
  --footer "$FOOTER"
```

The script replies with a link to the commit, then resolves each listed thread. Do not resolve a thread without a reply that shows where it was addressed. Leave a thread that you did not address open, with a reply that says why.

Do not also post a summary comment - the per-thread replies are already the complete record.

## Reviewers

```bash
GH_PAGER=cat gh pr edit <pr> --repo <owner>/<repo> --add-reviewer <user>
GH_PAGER=cat gh pr edit <pr> --repo <owner>/<repo> --remove-reviewer <old> --add-reviewer <new>
```

The reviewer's GitHub login is provided to you. Never assume or guess a handle or login - ask the requester when you were not given one.

## Media in PR bodies

Embed screenshots or video links in the PR body markdown. Never commit media files to the branch.
