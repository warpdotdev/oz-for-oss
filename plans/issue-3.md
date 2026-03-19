# Issue #3 — Search is case-sensitive and misses valid matches

## Problem
The `search` command uses `String.prototype.includes()` for matching, which is case-sensitive. Searching for `"buy"` does not find a todo with text `"Buy groceries"`.

## Current state
In `src/app.ts` (line 68), `searchTodos` filters with:

```ts
const results = todos.filter((todo) => todo.text.includes(query));
```

No normalization is applied to either `todo.text` or `query`, so the comparison is case-sensitive.

The CLI entry point (`src/index.ts`, lines 112-120) passes the raw user query string through without any case transformation.

## Proposed changes
**File:** `src/app.ts` — `searchTodos` function (line 68)

Normalize both sides of the comparison to lowercase before calling `includes`:

```ts
const results = todos.filter((todo) =>
  todo.text.toLowerCase().includes(query.toLowerCase())
);
```

Call `query.toLowerCase()` once before the filter loop for efficiency, e.g.:

```ts
const lowerQuery = query.toLowerCase();
const results = todos.filter((todo) => todo.text.toLowerCase().includes(lowerQuery));
```

No changes are needed in `src/index.ts`, `src/types.ts`, or `src/storage.ts`.

## Risks and considerations
- **Locale edge cases:** `toLowerCase()` handles standard ASCII well. Locale-sensitive comparisons (e.g., Turkish İ/i) are out of scope for this CLI app.
- **Performance:** Calling `toLowerCase()` on every todo text during each search is negligible for the expected data sizes of a local CLI todo list.
- **No tests exist:** The project has no test suite. Adding a basic test for case-insensitive search would be a good follow-up but is not strictly required by this issue.

## Open questions
None — the issue is well-specified and the fix is straightforward.
