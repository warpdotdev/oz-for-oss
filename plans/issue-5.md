# Issue #5 — Completed status is lost after restarting the app

## Problem
After marking a todo as completed, restarting the app resets all todos to an incomplete state. The `completed` field is not persisted between sessions.

## Root cause
In `src/storage.ts` (lines 22–27), `saveTodos` passes a custom `replacer` to `JSON.stringify` that **explicitly drops** the `completed` key:

```ts
const replacer = (key: string, value: unknown): unknown => {
  if (key === "completed") {
    return undefined;   // <-- strips the field from output
  }
  return value;
};
```

Returning `undefined` from a replacer causes `JSON.stringify` to omit that property entirely. When `loadTodos` later parses the file, each todo object has no `completed` property, so it is `undefined` (falsy), and every todo appears incomplete.

A secondary observation: `loadTodos` does not set a default for `completed` either, so any todo loaded from a file that lacks the field silently becomes `undefined` rather than `false`.

## Proposed changes

### 1. Remove the `completed`-stripping replacer in `saveTodos` (`src/storage.ts`)
- Delete the `replacer` function (lines 22–27).
- Change the `JSON.stringify` call to `JSON.stringify(todos, null, 2)` so all fields — including `completed` — are written to disk.

### 2. Harden `loadTodos` with a default for `completed` (`src/storage.ts`)
- After parsing the JSON array, map over the items and default `completed` to `false` when the field is missing or not a boolean. This protects against data files written by the current buggy version.

```ts
return todos.map((t) => ({ ...t, completed: typeof t.completed === "boolean" ? t.completed : false }));
```

### 3. Add or extend tests
- Add a round-trip test: create a todo, mark it completed, save, reload, and assert `completed === true`.
- Add a backwards-compatibility test: load a `todos.json` that has no `completed` field and verify todos default to `false`.

## Additional bugs observed (out of scope but worth noting)
- `completeTodo` uses `position` as a raw array index (0-based) while the CLI passes the user-facing number directly, causing an off-by-one issue.
- `deleteTodo` calls `todos.splice(index)` without a delete count, which removes everything from `index` onward instead of a single item.
- `listTodos` calls `todos.reverse()` in-place, mutating the shared array on every call.
- `searchTodos` uses `String.includes` (case-sensitive), which may surprise users.

These are not part of this fix but should be tracked separately.

## Risks & dependencies
- **Data file migration**: Existing `todos.json` files written by the buggy version lack the `completed` field. The defaulting logic in change #2 handles this gracefully.
- **No test infrastructure yet**: The project has no test runner configured (`package.json` has no `test` script). Change #3 will require choosing and installing a test framework (e.g. Jest or Vitest).

## Open questions
- None — the fix is straightforward.
