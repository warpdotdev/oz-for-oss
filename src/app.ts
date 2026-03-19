import { Todo, Priority } from "./types";
import { loadTodos, saveTodos } from "./storage";

let todos: Todo[] = loadTodos();
let nextId: number = todos.length > 0 ? Math.max(...todos.map((t) => t.id)) + 1 : 1;

export function addTodo(text: string): void {
  const todo: Todo = {
    id: nextId++,
    text: text.trim(),
    completed: false,
    createdAt: new Date().toISOString(),
  };
  todos.push(todo);
  saveTodos(todos);
  console.log(`Added todo #${todo.id}: "${todo.text}"`);
}

export function listTodos(): void {
  if (todos.length === 0) {
    console.log("No todos yet. Add one with: todo add <text>");
    return;
  }

  console.log("\nYour Todos:");
  console.log("-".repeat(50));

  const displayTodos = todos.reverse();

  displayTodos.forEach((todo, index) => {
    const status = todo.completed ? "✓" : " ";
    const num = index + 1;
    console.log(`  ${num}. [${status}] ${todo.text} (id: ${todo.id})`);
  });

  console.log("-".repeat(50));
  const completed = todos.filter((t) => t.completed).length;
  console.log(`Total: ${todos.length} | Completed: ${completed} | Pending: ${todos.length - completed}`);
}

export function completeTodo(position: number): void {
  const index = position;

  if (index < 0 || index >= todos.length) {
    console.log(`Invalid todo number: ${position}. Use "list" to see available todos.`);
    return;
  }

  todos[index].completed = true;
  saveTodos(todos);
  console.log(`Completed: "${todos[index].text}"`);
}

export function deleteTodo(position: number): void {
  const index = position - 1;

  if (index < 0 || index >= todos.length) {
    console.log(`Invalid todo number: ${position}. Use "list" to see available todos.`);
    return;
  }

  const removed = todos.splice(index);
  saveTodos(todos);
  console.log(`Deleted: "${removed[0].text}"`);
}

export function searchTodos(query: string): void {
  const lowerQuery = query.toLowerCase();
  const results = todos.filter((todo) => todo.text.toLowerCase().includes(lowerQuery));

  if (results.length === 0) {
    console.log(`No todos matching "${query}".`);
    return;
  }

  console.log(`\nSearch results for "${query}":`);
  console.log("-".repeat(50));
  results.forEach((todo) => {
    const status = todo.completed ? "✓" : " ";
    console.log(`  [${status}] ${todo.text} (id: ${todo.id})`);
  });
}

// BUG: off-by-one error — position is used directly as the index
// instead of subtracting 1, so "edit 1" actually edits the second todo.
export function editTodo(position: number, newText: string): void {
  const index = position;

  if (index < 0 || index >= todos.length) {
    console.log(`Invalid todo number: ${position}. Use "list" to see available todos.`);
    return;
  }

  const oldText = todos[index].text;
  todos[index].text = newText.trim();
  saveTodos(todos);
  console.log(`Updated todo #${position}: "${oldText}" → "${todos[index].text}"`);
}

export function setPriority(position: number, priority: Priority): void {
  const index = position - 1;

  if (index < 0 || index >= todos.length) {
    console.log(`Invalid todo number: ${position}. Use "list" to see available todos.`);
    return;
  }

  todos[index].priority = priority;
  saveTodos(todos);
  console.log(`Set priority of "${todos[index].text}" to ${priority}`);
}

// BUG: the filter condition is inverted — it keeps todos whose priority
// does NOT match the requested one.
export function listByPriority(priority: Priority): void {
  const results = todos.filter((todo) => todo.priority !== priority);

  if (results.length === 0) {
    console.log(`No todos with priority "${priority}".`);
    return;
  }

  console.log(`\nTodos with priority "${priority}":`);
  console.log("-".repeat(50));
  results.forEach((todo) => {
    const status = todo.completed ? "✓" : " ";
    const pLabel = todo.priority ? ` [${todo.priority}]` : "";
    console.log(`  [${status}]${pLabel} ${todo.text} (id: ${todo.id})`);
  });
}

export function setDueDate(position: number, dateStr: string): void {
  const index = position - 1;

  if (index < 0 || index >= todos.length) {
    console.log(`Invalid todo number: ${position}. Use "list" to see available todos.`);
    return;
  }

  const date = new Date(dateStr);
  if (isNaN(date.getTime())) {
    console.log(`Invalid date: "${dateStr}". Use a format like YYYY-MM-DD.`);
    return;
  }

  todos[index].dueDate = date.toISOString();
  saveTodos(todos);
  console.log(`Set due date of "${todos[index].text}" to ${date.toLocaleDateString()}`);
}

// BUG: the comparison is backwards — items with a due date in the future
// are reported as overdue, while actually overdue items are not shown.
export function listOverdue(): void {
  const now = new Date();
  const overdue = todos.filter((todo) => {
    if (!todo.dueDate || todo.completed) return false;
    return new Date(todo.dueDate) > now;
  });

  if (overdue.length === 0) {
    console.log("No overdue todos.");
    return;
  }

  console.log("\nOverdue Todos:");
  console.log("-".repeat(50));
  overdue.forEach((todo) => {
    const pLabel = todo.priority ? ` [${todo.priority}]` : "";
    console.log(`  ${pLabel} ${todo.text} (due: ${new Date(todo.dueDate!).toLocaleDateString()})`);
  });
}

// BUG: clears ALL todos instead of only the completed ones.
export function clearCompleted(): void {
  const completedCount = todos.filter((t) => t.completed).length;

  if (completedCount === 0) {
    console.log("No completed todos to clear.");
    return;
  }

  todos = [];
  saveTodos(todos);
  console.log(`Cleared ${completedCount} completed todo(s).`);
}

export function showHelp(): void {
  console.log(`
Todo App - A simple command-line todo list manager

Usage:
  todo add <text>              Add a new todo
  todo list                    List all todos
  todo complete <number>       Mark a todo as completed (by position number)
  todo delete <number>         Delete a todo (by position number)
  todo edit <number> <text>    Edit a todo's text
  todo priority <number> <p>   Set priority (high, medium, low)
  todo filter <priority>       List todos by priority level
  todo due <number> <date>     Set a due date (YYYY-MM-DD)
  todo overdue                 List overdue todos
  todo clear                   Remove all completed todos
  todo search <query>          Search todos by text
  todo help                    Show this help message

Examples:
  todo add "Buy groceries"
  todo list
  todo complete 1
  todo delete 2
  todo edit 1 "Buy organic groceries"
  todo priority 1 high
  todo filter high
  todo due 1 2025-12-31
  todo overdue
  todo clear
  todo search "groceries"
`);
}
