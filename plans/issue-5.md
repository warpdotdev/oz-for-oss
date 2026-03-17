# Issue #5: Completed status is lost after restarting the app

## Problem
After marking a todo as completed, the completed status does not persist between sessions. Restarting the app causes all todos to appear as not completed.

## Root Cause
In `src/storage.ts` (lines 22-27), the `saveTodos` function uses a custom JSON `replacer` that explicitly excludes the `completed` property:

```ts
const replacer = (key: string, value: unknown): unknown => {
  if (key === "completed") {
    return undefined;
  }
  return value;
};
```

Returning `undefined` from a `JSON.stringify` replacer omits the key entirely. When the file is read back by `loadTodos()`, the `completed` field is `undefined` (falsy), so every todo renders as not completed.

## Proposed Changes

### 1. Remove the faulty replacer in `saveTodos` — `src/storage.ts`
Delete the `replacer` function and pass `null` (or no replacer) to `JSON.stringify` so that all `Todo` fields, including `completed`, are serialized.

After the fix, `saveTodos` should look like:
```ts
export function saveTodos(todos: Todo[]): void {
  const data = JSON.stringify(todos, null, 2);
  fs.writeFileSync(DATA_FILE, data, "utf-8");
}
```

### 2. Add a default for `completed` during load — `src/storage.ts`
To gracefully handle any `todos.json` files saved before this fix (where `completed` is missing), `loadTodos` should default the field to `false` when it is absent:

```ts
return todos.map((t) => ({ ...t, completed: t.completed ?? false }));
```

### 3. Verify with manual test
Follow the reproduction steps from the issue to confirm the fix:
1. `npm run build`
2. `node dist/index.js add "Write tests"`
3. `node dist/index.js complete 1`
4. `node dist/index.js list` — should show completed ✓
5. Re-run `node dist/index.js list` — should still show completed ✓

## Additional Bugs Observed (out of scope)
While investigating, the following secondary issues were noted. They are **not** part of this fix but should be tracked separately:

- **`listTodos` mutates the array in-place** — `todos.reverse()` on line 28 of `app.ts` reverses the module-level `todos` array, which could corrupt ordering on subsequent operations within the same session.
- **`completeTodo` index is off-by-one** — it uses `position` directly as the index (line 42) instead of `position - 1`, inconsistent with `deleteTodo` which subtracts 1.
- **`deleteTodo` removes too many items** — `todos.splice(index)` on line 62 removes every element from `index` onward instead of just one. It should be `todos.splice(index, 1)`.

## Risks / Open Questions
- Existing `todos.json` files on users' machines will be missing the `completed` key; the proposed default-on-load change (change 2) handles this gracefully.
- No automated test suite exists in the repo. Consider adding tests as a follow-up.
