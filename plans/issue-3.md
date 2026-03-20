# Issue #3 — Search is case-sensitive and misses valid matches

## Problem
The `search` command performs a case-sensitive comparison, so searching for `"buy"` does not match a todo containing `"Buy groceries"`.

## Current state
In `src/app.ts` (line 68), `searchTodos` filters using `String.prototype.includes`, which is case-sensitive:

```ts
const results = todos.filter((todo) => todo.text.includes(query));
```

No other search or filter path in the codebase normalises case either, but the issue specifically targets the `search` command.

## Proposed changes
**File:** `src/app.ts` — `searchTodos` function (lines 67-81)

Convert both the todo text and the query to lowercase before comparing:

```ts
const lowerQuery = query.toLowerCase();
const results = todos.filter((todo) => todo.text.toLowerCase().includes(lowerQuery));
```

This is the minimal, focused fix. No changes are needed in `index.ts`, `storage.ts`, or `types.ts`.

## Risks / open questions
- **Locale sensitivity:** `toLowerCase()` uses the runtime's default locale. For this simple CLI app this is sufficient; `toLocaleLowerCase()` is unnecessary unless internationalisation is a stated goal.
- **Scope creep:** The `listByPriority` filter has a separate inversion bug (tracked by its own `BUG` comment). This plan intentionally does not address it.
- **No test suite exists.** Manual verification via the reproduction steps in the issue (`add "Buy groceries"` then `search "buy"`) is the only validation path today.
