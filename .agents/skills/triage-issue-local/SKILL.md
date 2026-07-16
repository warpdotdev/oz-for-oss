---
name: triage-issue-local
specializes: triage-issue
description: Repo-specific triage guidance for oz-for-oss. Only the categories declared overridable by the core triage-issue skill may be specialized here.
---

# Repo-specific triage guidance for `oz-for-oss`

This file is a companion to the core `triage-issue` skill. It does not
redefine the triage output schema, safety rules, or follow-up-question
contract. It only specializes the override categories the core skill
marks as overridable.

## Heuristics

- Distinguish observed symptoms from reporter hypotheses and proposed fixes.
- Before asking any follow-up question, first try to answer it yourself through code inspection, documentation lookup, or web search. Only ask questions that you cannot resolve on your own and that only the reporter would know.
- Ask targeted follow-up questions only for details the agent cannot derive itself and that materially improve triage confidence.
- Prefer issue-specific questions over generic "please share more info" requests.

## Label taxonomy

The label taxonomy for this repository is managed in `.github/issue-triage/config.json`. Prefer labels from that configuration, and avoid inventing new labels unless the prompt explicitly allows it.

`warp:needs-support` is a configured label for issues that cannot be resolved through OSS contributions (billing inquiries, plan changes, refund requests, subscription or account management, pricing questions, payment issues). When you triage such a report, request `warp:needs-support`, set `close_issue` to `true`, and direct the reporter to Warp support in `statements`; the workflow applies the label, posts the support-escalation comment, and closes the issue. Do not use this label or `close_issue` for issues that can be addressed via OSS contributions.

## Recurring follow-up patterns

No repo-specific follow-up patterns have been captured for this repository yet. `oz-for-oss` is not a terminal or desktop application, so the GPU/driver, window-manager, shell-integration, and similar runtime-environment sub-items that only make sense for those products do not belong here. The weekly `update-triage` loop will propose additions as maintainer overrides reveal recurring patterns that are actually specific to this repository.

## Owner-inference hints

No repo-specific owner-inference hints beyond `.github/STAKEHOLDERS` have been captured yet.
