---
name: Verbosity
description: Whether the agent's written, text output was proportionate to the work.
agents:
    - Linking Test Review Agent
    - Linking Test Foreman Agent
    - Linking Test Implement Agent
    - Linking Test Spec Agent
    - Linking Test Triage Agent
labels:
    - value: proportionate
      description: 'The agent''s output matched the work: most messages consistently said what happened and what comes next, no point was repeated or restated, and nothing the recipient needed was omitted.'
      score: 1
    - value: verbose
      description: 'The agent padded its output repeatedly: preamble restating the request, narration of what it was about to do, summaries re-describing a visible diff or an earlier summary, filler in place of plain statements, or a PR description whose length or template sections outstrip the diff it describes. The signal to noise ratio is poor.'
      score: 0.2
    - value: terse
      description: 'The agent under-communicated: it went silent through long stretches of work, finished without saying what changed or needs review, or dropped information the recipient needed to act.'
      score: 0.1
passingScore: 0.5
samplingRate: 10
model: gpt-5-6-luna-xhigh
selfImprovement: true
---
**Rubric**

Evaluate the agent's natural-language output to a human or another agent: status updates, progress messages, completion summaries, and prose in pull requests, reviews, or tracker comments. Not about diff size or tool-output length; code comment volume is scored under Code Quality. Judge from the recipient's perspective — someone who needs to know what happened and what to do next. The bullets below are common patterns, not an exhaustive checklist — judge anything else that leaves output disproportionate to the work.

Assess:

- **Proportionality.** Prose volume matched the complexity of the work. Judge a PR description against the diff it describes, not against the rest of the run — a bloated body sitting on top of otherwise tight status updates is still `verbose`, not averaged into `proportionate` by the messages around it.
- **Padding.** Preamble restating the request, narrating what's about to happen, closing summaries that re-describe a visible diff, or an optional or inapplicable template section kept only because the template listed it rather than because it says something this change adds. A section the template requires is exempt from that test — keeping it and answering it minimally is not padding — but its answer is not: an unnecessarily elaborate response to a required section is still padding. A multi-section PR body (Summary/Changes/Verification, a repo template) is not itself evidence of proportionality — judge what each section says, not whether the structure is filled in.
- **Redundancy.** Re-explaining the same point across messages, or summarizing a summary.
- **Inflation.** Filler, hedging, and embellishment ("comprehensive", "robust") in place of a plain statement.
- **Under-communication.** Long silent stretches, a completion message that doesn't say what changed or needs review, or dropped information the recipient needed.
- **Channel discipline.** Using an unrequested summary file to communicate when a message was the right vehicle.

**Reason**

One to three sentences quoting one representative passage — a padded opener, a redundant summary, a thin completion message — or, for `proportionate`, what kept it tight.
