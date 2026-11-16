---
name: code-review
description: "How to review a code change and how to write the findings: what to evaluate, the blocking rules, the severities, the finding style, and the shape of a review summary. Use whenever you evaluate a code change, whatever the findings are used for afterward."
---

# code-review

How to judge a code change and how to write the findings. This skill covers judgment and writing only. What happens with the findings afterward is the invoker's decision.

## What to evaluate

Evaluate every changed artifact against the `code-quality` skill at its path in the available skills catalog, which the author also wrote to. A departure from it is a finding; how much it blocks is below. Judge every test the change adds against Test value below, in addition to code-quality's Tests dimension.

Account for every dimension in the findings, the ones with nothing to report included; a dimension you did not examine is reported as unexamined, never as clean. A brief's emphasis adds to the dimensions, it never narrows them.

## Test value

A test must justify its presence. One that asserts implementation instead of behavior costs maintenance on every refactor and weakens the suite. Behavior lives at the system's public contract - what it promises the callers of a module, service, or component - not in how a class or function gets there internally; a test anchored to an internal method rather than that contract is coupled to a choice that may not survive the next refactor.

Ask of each test or case the change adds, where a table row or a subtest is a case: what behavior change would make this fail, and would that failure be a real defect no other test catches? When the honest answer is "a behavior-preserving refactor", "nothing", or "a higher-level test already covers it", it is a finding. A finding may target one assertion and leave the test.

Answer it against the production code, never against the test's name or shape. You will usually be reaching that answer because the assertion restates a literal or a constant from the source; because it checks what the framework already enforces, such as a not-called assertion on a mock that already fails on any unexpected call; because the subject is a getter, a default, or plain construction; because another case already reaches the same branch; because the test asserts a call sequence or private state instead of an outcome; or because the test's setup is dominated by wiring a dependency-injection container rather than exercising the code through its public entry point. Those are conclusions to arrive at, not patterns to match.

When the answer is "nothing", check whether the behavior the test *names* is real and uncovered before proposing removal. An assertion that only runs inside an `if`, or a case pinning the pass-through while skipping the error branch, is a coverage finding: the test does not do its job and the correction is to make it. A deletion finding carries any coverage gap it exposes.

Identify the least destructive correction that removes the problem: fix the assertion, narrow the case to the distinct path it adds, move the test to the level where the behavior is observable, delete. Name the rung and why the one above it does not work; an unexplained "delete" is not a correction. Most findings are the first rung.

Never flag these. Each is a case where the thing that looks redundant is the only expression of the behavior.
- A case pinning a contract another system depends on where nothing else fails when the value silently changes - a flag spelling, a wire format, a telemetry key, the absence of an interface implementation - provided the case observes the value where that consumer reads it. "Another system" means outside the unit's compile-time reach, not outside the repository.
- A call assertion where the collaborator is the unit's only observable output. This includes a not-called assertion where non-invocation is the behavior under test and nothing else fails when the call reappears: the framework's panic is not a substitute for the test stating what it guards.
- A class or markup assertion where the rendered class is all the unit exposes. Where no styles compute, it is the only observable output. Flag one only when the same behavior is visible in accessible state, role, or text - and then the correction is to assert that instead, not to delete.
- Real IO where the IO is the subject rather than incidental to the logic.
- A near-identical case that reaches a different branch or pins a boundary the others do not.
- A small test. The burden is "what defect does this catch", never "is this test big enough".

Production seams. Flag indirection that exists so a test can reach in only when the behavior is reachable at comparable cost without it, and only when you can name what replaces the tests the seam carries. A seam that is the only affordable route to the behavior stands, whatever its motive.

`important` when the test imposes ongoing cost or the correction is a rewrite; `suggestion` when the correction is removal and nothing of value goes with it. This gate does not relax the coverage obligation: a bug fix still needs its regression test. Where the repository has its own testing skills, read them and apply what they add, after checking their claims still hold against the repository; they do not displace the exemptions above.

## Blocking rules

- Always blocking: a correctness problem, a security problem, a change that does not build, a failing test, red required CI, and missing or mismatched visual proof on a user-facing change.
- Blocking only when the harm is material: standards, complexity, naming, and comments.
- A low-value test blocks only when the harm is material: an implementation-coupled test that will break on the next refactor, or a production seam whose behavior is reachable at comparable cost without it. Never block on test volume alone, and never make "too many tests" a finding by itself.
- For a clear first version, frame robustness requests (timeouts, retries, lifecycle handling) as optional future work, unless correctness, security, or data loss is at risk.

## Severities

- critical: bugs, security problems, crashes, data loss.
- important: logic problems, edge cases, missing error handling.
- suggestion: a worthwhile improvement, a better pattern, or a low-value comment that should be removed.
- nit: naming, formatting, and comments that are merely awkward or verbose. Include a nit only with a concrete correction.
- question: an open question about intent, rationale, or design that needs a decision. A question is always human-facing.

## Writing findings

- Structure each finding: the problem, its impact, and the correction. Three sentences at most. No compliments, no hedging, no process narration.
- Tie each finding to a file and a line in the change when possible. Mark a finding that has no changed line (untouched code, a missing artifact, the change description) as a summary-level finding.
- Propose an exact replacement when you know the correct code.
- Review the change, not the whole repository. A problem in untouched code is a note, not a blocking finding.
- Flag a change description that reads like a work chronicle or a file inventory. The description must state the problem and the net outcome. A list of files, functions, or commits is a finding: that inventory belongs in the diff.

## Shape of a posted review

- Overview: at most two sentences. What the change does, and the net review position.
- Concerns: the summary-level findings, each at most three sentences. Omit the section when it is empty.
- Verdict: one line of check results (build, tests, CI, visual proof), the finding counts by severity, and the recommendation.

Structure the posted review with this template:

```markdown
## Overview
What the change does, and the net review position (approve or request changes). At most two sentences.

## Concerns
- Each summary-level finding: the problem, its impact, and the correction. At
  most three sentences per finding. Use a bulleted list.

## Verdict
Checks: build <pass|fail>, tests <pass|fail>, CI <green|red>, visual proof
<present|missing|n/a>

Found: <n> critical, <n> important, <n> suggestions, <n> nits
```

Use inline comments for findings that are tied to a specific line in the change.
