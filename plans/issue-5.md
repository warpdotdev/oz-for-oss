# Issue #5 — Completed status is lost after restarting the app

## Problem
After marking a todo as completed, restarting the app causes all todos to appear incomplete. The `completed` field is not persisted to `todos.json`.

## Root cause
In `src/storage.ts` (lines 22-27), the `saveTodos` function passes a custom `replacer` to `JSON.stringify` that explicitly excludes the `completed` key:

```ts
const replacer = (key: string, value: unknown): unknown => {
  if (key === "completed") {
    return undefined;
  }
  return value;
};
```

Returning `undefined` from a JSON replacer causes that property to be omitted from the output. When `loadTodos` later reads the file, the missing `completed` field defaults to `undefined` (falsy), so every todo appears incomplete.

## Current-state observations
- `Todo` interface (`src/types.ts`) correctly defines `completed: boolean`.
- `loadTodos` (`src/storage.ts`) deserializes the JSON without setting a default for `completed`, so any missing field becomes `undefined`.
- `completeTodo` (`src/app.ts:49`) sets `completed = true` and calls `saveTodos`, but the write strips the field.

## Proposed changes

### 1. Remove the replacer in `saveTodos` (`src/storage.ts`)
Delete the `replacer` function and pass `null` (or omit the argument) as the replacer to `JSON.stringify`:

```ts
const data = JSON.stringify(todos, null, 2);
```

This is the only change required to fix the reported bug.

### 2. (Recommended) Default `completed` to `false` on load (`src/storage.ts`)
As a defensive measure, map loaded todos so that a missing `completed` field falls back to `false`:

```ts
return todos.map((t) => ({ ...t, completed: t.completed ?? false }));
```

This protects against any existing `todos.json` files written before the fix.

## Additional bugs noticed (out of scope for this issue)
These are not part of the reported problem but were observed during investigation and may warrant separate issues:

- **`listTodos` mutates the array in place** — `todos.reverse()` on line 28 of `app.ts` reverses the shared `todos` array, corrupting order on repeated calls.
- **`completeTodo` uses raw position as index** — `const index = position;` (line 42) differs from `deleteTodo` which uses `position - 1` (line 55), causing an off-by-one inconsistency.
- **`deleteTodo` splices to end** — `todos.splice(index)` without a delete-count removes everything from `index` onward instead of a single item.

## Risks and open questions
- **Existing data files**: Users who already have a `todos.json` without `completed` fields will still see all todos as incomplete until they re-complete them. The defensive default in proposed change #2 handles this gracefully but does not restore previously-lost state.
- **No automated tests**: The project has no test infrastructure. Adding a test for the save/load round-trip would prevent regressions but is out of scope for this plan.
