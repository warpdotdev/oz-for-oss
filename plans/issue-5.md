# Issue #5 — Completed status is lost after restarting the app

## Problem
After marking a todo as completed, the completed status does not survive an app restart. Running `list` after restarting shows all todos as not completed.

## Root cause
In `src/storage.ts` (lines 22-26), the `saveTodos` function passes a custom `replacer` to `JSON.stringify` that explicitly removes the `completed` property:

```ts
const replacer = (key: string, value: unknown): unknown => {
  if (key === "completed") {
    return undefined;
  }
  return value;
};
```

Returning `undefined` from a replacer causes `JSON.stringify` to omit that key entirely. As a result, `todos.json` never contains `completed` fields. When `loadTodos` reads the file back, each todo object lacks the `completed` property, which evaluates as `undefined` (falsy), so every todo appears incomplete.

## Proposed changes

### 1. Remove the `completed`-stripping replacer in `saveTodos` (`src/storage.ts`)
Delete the custom `replacer` function and pass `null` (or no replacer) to `JSON.stringify` so that all `Todo` properties — including `completed` — are serialized.

**Before:**
```ts
const replacer = (key: string, value: unknown): unknown => {
  if (key === "completed") { return undefined; }
  return value;
};
const data = JSON.stringify(todos, replacer, 2);
```

**After:**
```ts
const data = JSON.stringify(todos, null, 2);
```

### 2. Default `completed` to `false` on load (`src/storage.ts`)
As a defensive measure, normalise todos when loading so that any previously-saved records that lack a `completed` field default to `false`. This ensures backward compatibility with existing `todos.json` files written by the buggy version.

**Suggested addition in `loadTodos`, after `JSON.parse`:**
```ts
return todos.map((t) => ({ ...t, completed: t.completed ?? false }));
```

### 3. Add a test for persistence round-trip
Add a test (or script) that:
1. Creates a todo.
2. Marks it as completed.
3. Saves to disk.
4. Reloads from disk.
5. Asserts the todo is still completed.

This can be a simple Node script or a proper test if a test framework is introduced.

## Files to modify
- `src/storage.ts` — primary fix (remove replacer, add default on load)

## Risks and notes
- **Data migration**: Existing `todos.json` files created by the buggy version will not have `completed` fields. The defensive default in step 2 handles this gracefully — no manual migration required.
- **No other callers of the replacer**: The replacer is only used inside `saveTodos`, so removing it has no side-effects elsewhere.
- **Additional bugs observed (out of scope for this issue)**:
  - `listTodos` calls `todos.reverse()` which mutates the module-level array in-place, corrupting order on repeated calls.
  - `completeTodo` uses the raw `position` as an array index instead of `position - 1`, making it 0-indexed while the UI is 1-indexed.
  - `deleteTodo` calls `todos.splice(index)` without a count argument, removing all items from that index onward instead of just one.
  These should be tracked as separate issues.
