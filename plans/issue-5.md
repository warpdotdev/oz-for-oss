# Issue #5: Completed status is lost after restarting the app

## Problem
After marking a todo as completed, restarting the app causes all todos to appear as not completed. The `completed` field is not persisted to disk.

## Root Cause
In `src/storage.ts` (lines 22–27), `saveTodos` uses a custom `replacer` function passed to `JSON.stringify` that explicitly excludes the `completed` property:

```ts
const replacer = (key: string, value: unknown): unknown => {
  if (key === "completed") {
    return undefined;   // <-- drops the field from output
  }
  return value;
};
```

When `loadTodos` later reads the file, each todo object has no `completed` key, so the value is `undefined` (falsy), and every item renders as incomplete.

## Proposed Changes

### 1. Remove the `replacer` that strips `completed` (`src/storage.ts`)
Delete the custom `replacer` function and call `JSON.stringify(todos, null, 2)` so that all fields — including `completed` — are serialized.

### 2. Add a defensive default in `loadTodos` (`src/storage.ts`)
When deserializing, default `completed` to `false` for any todo that is missing the field. This protects against existing `todos.json` files that were written before the fix:

```ts
return todos.map(t => ({ ...t, completed: t.completed ?? false }));
```

### 3. Add a persistence round-trip test
Add a test (or script) that:
1. Saves a todo list containing a completed item.
2. Reads it back.
3. Asserts the completed flag is preserved.

This can live in a new `src/__tests__/storage.test.ts` (or a lightweight script) depending on whether a test framework is introduced. At minimum, a manual verification script is acceptable for this small project.

## Other Bugs Observed (out of scope, noted for awareness)
- `listTodos` mutates the `todos` array in place via `todos.reverse()` (`src/app.ts:28`).
- `completeTodo` uses `position` directly as the array index instead of `position - 1`, inconsistent with `deleteTodo` (`src/app.ts:42` vs `src/app.ts:55`).
- `deleteTodo` calls `todos.splice(index)` without a count argument, removing all items from `index` onward (`src/app.ts:62`).

These should be tracked as separate issues.

## Risks / Open Questions
- Existing `todos.json` files in the wild will lack the `completed` field; the defensive default in `loadTodos` handles this gracefully.
- No test framework is currently configured. The plan proposes a minimal verification approach; a full test setup (e.g., Jest or Vitest) could be added as follow-up work.
