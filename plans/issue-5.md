# Issue #5 — Completed status is lost after restarting the app

## Problem
After marking a todo as completed, restarting the app causes all todos to appear as not completed. The `completed` status does not persist between sessions.

## Root cause
In `src/storage.ts`, the `saveTodos` function passes a custom `replacer` to `JSON.stringify` that explicitly drops the `completed` key:

```ts
const replacer = (key: string, value: unknown): unknown => {
  if (key === "completed") {
    return undefined;   // ← strips the field from the JSON output
  }
  return value;
};
```

Returning `undefined` from a replacer tells `JSON.stringify` to omit that property. So the on-disk `todos.json` never contains `completed`, and on next load every todo's `completed` field is `undefined` (falsy).

## Current-state observations
- `src/types.ts` — `Todo` interface includes `completed: boolean`.
- `src/storage.ts:saveTodos` — serializes with the faulty replacer described above.
- `src/storage.ts:loadTodos` — deserializes with `JSON.parse` but does not default missing fields, so a missing `completed` key becomes `undefined`.
- `src/app.ts:completeTodo` — correctly sets `todos[index].completed = true` and calls `saveTodos`, but the write discards the value.

## Proposed changes

### 1. Remove the faulty replacer in `saveTodos` (`src/storage.ts`)
Delete the `replacer` function and pass `null` (or no replacer) to `JSON.stringify` so that `completed` is included in the serialized output:

```ts
const data = JSON.stringify(todos, null, 2);
```

This is the only production code change required.

### 2. Add a defensive default in `loadTodos` (`src/storage.ts`)
As a safety net, map loaded objects to ensure `completed` defaults to `false` when missing (handles pre-fix data files):

```ts
return todos.map(t => ({ ...t, completed: t.completed ?? false }));
```

### 3. Add a regression test
Create a test (location TBD based on project conventions) that:
1. Saves a todo with `completed: true` via `saveTodos`.
2. Reads it back via `loadTodos`.
3. Asserts `completed` is `true`.

No test framework is currently configured; the implementer should add one (e.g. Jest or Vitest) or use a minimal Node assert-based script.

## Risks & open questions
- **Existing data files**: Users who already have a `todos.json` without `completed` fields will see all items default to `false` after the fix. This matches their current (broken) experience, so no data is truly lost, but it is worth noting.
- **No test infrastructure**: The project has no test runner configured yet. The implementer should decide whether to add one or use a lightweight approach.
- **`listTodos` mutates in place**: `src/app.ts:28` calls `todos.reverse()` without copying, which mutates the module-level array on every `list` call. This is a separate bug but worth flagging since it affects display order between commands in the same session.
