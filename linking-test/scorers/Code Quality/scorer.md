---
name: Code Quality
description: Whether the code, tests, and comments the agent produced are well-designed and consistent with the target repo's conventions.
agents:
    - Linking Test Implement Agent
labels:
    - value: approve
      description: The agent produced well-designed, correct code that is consistent with repo conventions, adequately tested, and clean of smells; a senior reviewer would approve it outright, with at most trivial nits.
      score: 1
    - value: block
      description: 'The agent produced code with at least one defect a reviewer would insist on fixing before merge: a correctness or concurrency bug, a missed edge case, an inconsistent pattern, weak or missing tests, or mixed-in artifacts that do not belong.'
      score: 0.2
    - value: insufficient_evidence
      description: The transcript shows no code diff, or too little of one to judge.
      score: 0.5
passingScore: 0.5
samplingRate: 10
model: gpt-5-6-luna-xhigh
selfImprovement: true
---
**Rubric**

This scorer applies to agent runs that produce code. Evaluate the actual code artifact — the diff itself, not the process used to produce it. Judge it the way a careful senior reviewer would review the same pull request, using the target repo's own established conventions as the standard. The verdict is binary: `approve` means that reviewer would merge the diff as-is, with at most trivial nits; if they would insist on a fix before merging (one real defect is enough), the verdict is `block`. The bullets below are common quality dimensions, not an exhaustive checklist — judge any other way the artifact falls short of what a careful senior engineer would ship.

Assess:

- **Design.** The shape of the change fits the codebase; it isn't premature abstraction, scope creep, or a change that belongs somewhere else (a library, a config value, a separate service).
- **Correctness.** The change does what it claims, including edge cases (nil/empty inputs, boundaries) and concurrency safety (races, unsafe shared state, goroutines spawned through the repo's async helpers rather than a bare `go` statement, with per-request context such as `*gin.Context` copied before the goroutine starts, never inside it).
- **Complexity.** No function, type, or expression is doing more than it needs to; no speculative genericity or indirection added for a need that doesn't exist yet.
- **Repo conventions.** Follows the same idioms as similar code in the same package — error handling (the repo's designated error library and classification, not ad-hoc errors; never returning `(nil, nil)` from a pointer/interface-and-error function — absence is a classified error or an explicit `found` bool), type placement (shared types in leaf packages, not wherever is convenient), and any other established pattern. An unexplained deviation from a clear local pattern is a defect even if the new code works.
- **Code smells.** Magic numbers or strings without a named constant, copy-paste that should be a shared function, commented-out code, vague or stale TODOs, workarounds that patch a symptom instead of the root cause, silently swallowed errors, deep nesting that early returns would flatten.
- **Tests.** Present for the change, and actually verify the behavior they claim to (a broken implementation would fail them) rather than asserting trivia or mocking away the logic under test; cover the error and edge paths, not just the happy path; not so tightly coupled to internals that unrelated changes would break them.
- **Naming.** Every new identifier communicates what it represents, at a length that's unambiguous without being noisy.
- **Comments.** Explain why, not what; a comment carries a maintenance cost, so it earns its place only where the code cannot speak for itself, and the reader is a senior engineer — when the names already say how the code works, a comment saying it again is a defect, as is a narration of the syntax line by line ("initialize the array", "loop over the users"). A doc comment on an exported symbol describes its purpose and constraints without narrating its implementation or naming its callers; a doc comment on a container (struct, class, enum, trait, interface) describes the item as a whole rather than enumerating or re-explaining its members, and a member's own doc comment explains that member without restating the container's. No comment describes an edit, a refactor, or a prior state of the code rather than its current behavior; a comment already given once in a doc comment isn't repeated at each call site; an existing comment isn't deleted or rewritten by a change unrelated to it, and is edited only when the logic it describes changed.
- **Diff hygiene.** The diff contains only the changes that are intended to be committed — no temporary or transient text, screenshots, scratch scripts, or other verification scaffolding committed alongside it.
- **Documentation.** READMEs, guides, or API docs are updated in the same change when the change affects how the software is built, tested, or used.
- **Corrections.** A defect the orchestrator raised mid-run — relaying a finding from code review or a report from the user — counts against the artifact regardless of whether it was ultimately fixed; needing an external correction at all is a negative signal, not just a defect left unresolved. Judge only genuinely corrective content: a routine status update, a handoff note, or a claim in the orchestrator's message that is itself wrong doesn't count against the artifact just because it came from the orchestrator.
Out of scope: whether the implementation process followed the playbook or did what was asked (scored under Task Compliance), and rule-following and skill use, including mechanical conventions like commit format or branch naming (scored under Procedure Compliance). Judge the artifact on its own merits.

**Reason**

One to three sentences citing the specific file, pattern, or defect that drove the grade — quote or closely paraphrase the line or convention at issue. Name what would have caught it: a lint rule, a repo convention the agent should have searched for, a test case. For `insufficient_evidence`, say what you couldn't see — for example, no diff was produced, or the diff wasn't in the transcript.
