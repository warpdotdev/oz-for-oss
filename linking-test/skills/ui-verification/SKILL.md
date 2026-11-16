---
name: ui-verification
description: "Judgment for producing visual proof of a running UI with the computer-use tool: what to capture, what counts as proof, where the captures must land (PR descriptions, comments, messages), and how to refresh them. Use whenever you exercise a running interface — a web app in a browser, a desktop client, or any other rendered surface — to verify a user-facing change, reproduce a UI bug, or check a PR's visual claims."
---

# ui-verification

How to produce visual proof when testing a running UI. The mechanics of the tools involved — the `computer_use` session rules and capture-policy fields, and the parameters of `get_artifacts_for_pull_request_description` and `get_media_artifact_links` — are defined by the tools themselves and the base prompt. This skill does not restate them; it covers what the tools do not decide: what to capture, what counts as proof, and where the proof must land. When visual proof is required at all is the invoking procedure's decision.

## Capture policy

- Default to video: set `video_policy` to `require` and describe the flow to record in `video_instructions`. A recording of the real interaction is the strongest proof.
- Screenshots complement a recording; they are not only a fallback. Require both when a still adds something the flow moves past: the final rendered state, a key dialog, a visual detail worth inspecting closely. Describe those states in `screenshot_instructions`. Screenshots as the only medium are for a genuinely static render with no interaction to show.
- Captures are reported as run artifacts on the server. Never commit media to the branch.
- When the environment provides a dedicated skill for testing the target surface, read it and follow it on top of this procedure.

## What counts as proof

- Exercise the real path on the actual rendered surface. A mockup, a design image, or an adjacent screen is not proof.
- For a bug reproduction, capture the symptom occurring. For a fix, capture the same path with the symptom gone. For a new feature, capture the new surface and its key interactions.
- The capture must show the exact surface, state, and behavior in question. Proof of a wrong path, an incomplete state, or a missing criterion is missing proof. Run the session again when the captures fall short.

## Platforms

- `$OZ_CLI` is the path or command the runtime exports for the run. Never hardcode the binary name - it varies by channel.
- The available runners are the source of truth for the platforms that cloud computer use can target. That inventory changes over time, so never bake a fixed platform verdict into a skill, a report, or an issue. Consult the runners at decision time: `"$OZ_CLI" runner list --output-format json`, reading each runner's `os` (and `arch` when it matters).
- Name the specific platform you exercised in your report. Offer, imply, or present only a platform that has a matching runner in that output, never one from memory. For example, do not offer Windows verification when the list has no Windows runner.

## Where the proof goes

- A PR description carries the proof via `get_artifacts_for_pull_request_description`: insert the returned managed blocks into the PR body, videos above screenshots.
- Every other destination (a PR or issue comment, a tracker comment, a Slack message, a chat reply) carries links from `get_media_artifact_links`. Post the relevant subset, formatted for the destination.
- Call either tool only after the computer-use work is done: the result reflects the captures reported so far, and a later session requires a new call.
- An empty result means no media was captured. Do not fabricate links. Run the session again, or report that the proof is missing.

## Refreshing proof on a PR

- When new captures follow an earlier PR body — a rework, a follow-up change, a re-verification — fetch the artifacts again and replace the entire existing managed block with the returned one. The returned block is complete on its own; keep at most one video block and one screenshot block.
- Stale proof next to fresh proof misleads the reviewer. Carry an earlier capture forward only when it documents a part of the change that the new pass did not touch.

## When the UI cannot be exercised

- When the surface genuinely cannot be reached — the app cannot launch, a credential is missing, the environment cannot render it — say so explicitly wherever the proof would have gone: your report, the PR description, the issue. Record the blocker and the next step.
- Never claim a visual verification that did not happen, and never substitute a code walkthrough for a capture. The verification stays outstanding until the rendered behavior is proven.
