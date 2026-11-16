---
name: code-quality
description: "The standard a code change is held to: correctness, standards, complexity, naming, comments, tests, and security. Use when writing a code change and when judging one, so the author and the reviewer work from one definition."
---

# code-quality

The standard a change is held to. The author writes to it; the reviewer judges against it.

The repository's own agent-facing guidance outranks this file: a root `AGENTS.md`, `WARP.md`, or `CLAUDE.md`, and any directory-scoped equivalent nearer the files. Find it first. Where it is specific and this is general, it wins.

## Dimensions

- Correctness: does what it intends, edge cases and error handling included.
- Standards: the repository's and the language's conventions, idioms, lint and format rules, over personal preference.
- Complexity: no needless complexity, dead code, over-long functions, or meaningful performance problems; prefer the simpler equivalent. Setup that only exists to exercise the change by hand does not belong in the diff.
- Naming: clear, accurate, consistent.
- Comments: a comment must be intelligible without looking anything up — this PR, the review, a ticket, or an earlier revision — and it must tell the reader something the code doesn't already say; either failure means delete it. The reader is a senior engineer: a comment that says again what the names already say, or narrates the syntax line by line ("initialize the array", "loop over the users"), is redundant. Keep a local view: not a caller, not another unit's internals. Past behavior belongs only when it prevents a likely regression, and then only as what breaks. Stale, misleading, and redundant comments are defects. The doc comment on a container (struct, class, enum, trait, interface) describes the item as a whole, not its members; each member's own doc comment explains that member without restating the container's. An existing comment is deleted or rewritten only when the logic it describes changed.
- Tests: a bug fix carries a regression test that fails before and passes after; a feature covers its behavior and edge cases. Exempt: config-only, dependency or version bumps, constant or flag defaults, pure data or copy. A test that only varies already-covered inputs adds nothing. Production code never grows a seam to make a test possible — a swappable package-level variable, an exported internal, a tunable interval — and finding the same seam elsewhere in the repository argues that it spread, not that it is right.
- Security: injection, authentication and authorization, secret handling, unsafe input.
