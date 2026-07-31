---
name: evaluate-auto-implement-eligibility
description: >
  Evaluates whether a triaged GitHub issue is eligible for the factory
  auto-implement queue. Use during issue triage after the primary
  triage pass is complete.
---

# Factory Auto-Implement Eligibility

Determine whether this issue is a candidate for the factory
auto-implement queue. When eligible, the workflow adds the
`factory-auto-implement` label only. It does **not** assign `oz-agent`
and does **not** apply the creation-time `auto-implement` label (that
label is reserved for trusted issues labeled at open time, which skip
triage entirely). The bar is deliberately high.

## Decision rule

All of the following gates must pass. If any gate fails, or if you are
uncertain about any gate, the answer is `false`.

### Gate 1: Bug only
The issue describes an observed behavior that is clearly wrong.
Enhancements, feature requests, performance improvements, and vague
improvement requests are never eligible, even if fully specified.

### Gate 2: Visual bugs must be objectively wrong, not a matter of taste
Exclude issues where the desired outcome is a judgment call — e.g.
"this button should be a different color", "this spacing feels off",
or any report that expresses a personal opinion about appearance.
A visual bug is eligible only when the correct state is unambiguous:
a missing icon, a broken layout that clearly corrupts the UI, text
that is visibly truncated or overlapping, or a rendering glitch with
a clear non-rendering root cause. When in doubt, exclude it.

### Gate 3: Root cause known with high or medium confidence
The triage investigation must have identified a plausible root cause and
relevant code. If the root cause is unknown or speculative, the answer
is `false` — even if reproducibility is high.

### Gate 4: Unambiguous definition of done
There is exactly one reasonable interpretation of what "fixed" means.
A maintainer reading the issue would agree on the expected outcome
without discussion. Exclude anything where the correct fix depends on
a product judgment call.

### Gate 5: Client-side only
The fix touches only the client-side desktop app. Exclude any issue
involving: server or API, authentication, billing, cloud sync, session
sharing, telemetry backend, or the Oz platform backend. When in doubt,
the answer is `false`.

### Gate 6: Localized scope
The fix touches one component or subsystem. Exclude issues requiring
cross-cutting changes, modifications to shared core primitives, or
anything that would move architectural boundaries.

### Gate 7: Not security- or data-sensitive
The issue does not touch auth, permissions, billing, or data integrity.
The failure mode if the implementation is wrong is recoverable
(behavioral glitch, not data loss or a security hole).

### Gate 8: Small estimated effort
The fix path is apparent from the issue description and likely touches
a small number of files. A confident engineer could complete it in a
few hours without needing to understand a large or unfamiliar subsystem.

## Output

Add these two fields to `triage_result.json`:

```json
{
  "factory_auto_implement": true,
  "factory_auto_implement_reasoning": "one sentence naming the determining factor"
}
```

Always populate both fields. When `false`, `factory_auto_implement_reasoning`
must name the specific gate that failed. When `true`, briefly state the
positive case (e.g. "client-side crash with known root cause, clear fix
path, and no follow-up questions needed").

Do not emit the legacy `auto_implement` / `auto_implement_reasoning`
fields. Do not request `oz-agent` assignment or the `auto-implement`
label from this skill.

## Default

When in doubt, the answer is `false`. A missed candidate is a minor
inefficiency. A false positive queues factory work that should not run,
wasting compute and potentially producing an unwanted draft PR.
