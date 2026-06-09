"""Tests for ``core.routing``.

The webhook router owns every issue-driven and PR-driven Oz workflow
that the deleted ``.github/workflows/`` adapters used to host. These
tests cover the routes the webhook actually delivers and confirm that
out-of-band variants (non-Oz assignees, mismatched labels, etc.) are
dropped with a descriptive reason rather than dispatched anyway.
"""

from __future__ import annotations

import unittest

from . import conftest  # noqa: F401

from core.routing import (
    AUTO_IMPLEMENT_LABEL,
    OZ_AGENT_LOGIN,
    RouteDecision,
    WORKFLOW_ACKNOWLEDGE_UNKNOWN_MENTION,
    WORKFLOW_ANNOUNCE_READY_ISSUE,
    WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
    WORKFLOW_CREATE_SPEC_FROM_ISSUE,
    WORKFLOW_PLAN_APPROVED,
    WORKFLOW_RESPOND_TO_PR_COMMENT,
    WORKFLOW_REVIEW_PR,
    WORKFLOW_TRIAGE_NEW_ISSUES,
    WORKFLOW_VERIFY_PR_COMMENT,
    find_unrecognized_agent_mention,
    has_agent_mention,
    route_event,
)


def _issue(*, labels=None, assignees=None, pull_request=None, user=None):
    return {
        "number": 42,
        "labels": [{"name": label} for label in labels or []],
        "assignees": [{"login": login} for login in assignees or []],
        "user": user or {"login": "alice", "type": "User"},
        **({"pull_request": pull_request} if pull_request else {}),
    }


def _comment(*, body, login="alice", user_type="User"):
    return {
        "id": 1,
        "body": body,
        "user": {"login": login, "type": user_type},
        "author_association": "MEMBER",
    }


class IssuesEventTest(unittest.TestCase):
    """``issues`` events route to the triage workflow."""

    def test_issues_opened_routes_to_triage(self) -> None:
        decision = route_event("issues", {"action": "opened", "issue": _issue()})
        self.assertEqual(decision.workflow, WORKFLOW_TRIAGE_NEW_ISSUES)

    def test_issues_opened_with_auto_implement_routes_to_create_implementation(
        self,
    ) -> None:
        decision = route_event(
            "issues",
            {
                "action": "opened",
                "issue": _issue(labels=[AUTO_IMPLEMENT_LABEL]),
            },
        )
        self.assertEqual(
            decision.workflow, WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE
        )
        self.assertIn(AUTO_IMPLEMENT_LABEL, decision.reason)

    def test_issues_opened_on_triaged_issue_still_routes_to_triage(self) -> None:
        # Even issues that already carry post-triage labels (``triaged``,
        # ``ready-to-spec``, ``ready-to-implement``) should get a fresh
        # triage pass when re-opened so the bot picks up any state
        # changes that landed while the issue was closed.
        decision = route_event(
            "issues",
            {"action": "opened", "issue": _issue(labels=["triaged"])},
        )
        self.assertEqual(decision.workflow, WORKFLOW_TRIAGE_NEW_ISSUES)

    def test_issues_opened_on_ready_to_implement_issue_routes_to_triage(self) -> None:
        decision = route_event(
            "issues",
            {
                "action": "opened",
                "issue": _issue(labels=["triaged", "ready-to-implement"]),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_TRIAGE_NEW_ISSUES)

    def test_issues_opened_for_pull_request_is_dropped(self) -> None:
        decision = route_event(
            "issues",
            {"action": "opened", "issue": _issue(pull_request={"url": ""})},
        )
        self.assertIsNone(decision.workflow)

    def test_issues_opened_for_bot_author_is_dropped(self) -> None:
        decision = route_event(
            "issues",
            {
                "action": "opened",
                "issue": _issue(user={"login": "dependabot[bot]", "type": "Bot"}),
            },
        )
        self.assertIsNone(decision.workflow)

    def test_issues_opened_for_slack_feedback_bot_without_allowlist_is_dropped(
        self,
    ) -> None:
        decision = route_event(
            "issues",
            {
                "action": "opened",
                "issue": _issue(
                    user={
                        "login": "warp-dev-github-integration[bot]",
                        "type": "Bot",
                    }
                ),
            },
        )
        self.assertIsNone(decision.workflow)

    def test_issues_opened_for_configured_bot_author_routes_to_triage(self) -> None:
        decision = route_event(
            "issues",
            {
                "action": "opened",
                "issue": _issue(
                    user={
                        "login": "warp-dev-github-integration[bot]",
                        "type": "Bot",
                    }
                ),
            },
            triage_bot_author_allowlist=frozenset(
                {"warp-dev-github-integration[bot]"}
            ),
        )
        self.assertEqual(decision.workflow, WORKFLOW_TRIAGE_NEW_ISSUES)

    def test_issues_opened_for_configured_bot_author_login_is_case_insensitive(
        self,
    ) -> None:
        decision = route_event(
            "issues",
            {
                "action": "opened",
                "issue": _issue(
                    user={
                        "login": "Warp-Dev-GitHub-Integration[Bot]",
                        "type": "Bot",
                    }
                ),
            },
            triage_bot_author_allowlist=frozenset(
                {"warp-dev-github-integration[bot]"}
            ),
        )
        self.assertEqual(decision.workflow, WORKFLOW_TRIAGE_NEW_ISSUES)

    def test_issues_opened_with_auto_implement_from_bot_routes_to_create_implementation(
        self,
    ) -> None:
        decision = route_event(
            "issues",
            {
                "action": "opened",
                "issue": _issue(
                    labels=[AUTO_IMPLEMENT_LABEL],
                    user={"login": "trusted-intake[bot]", "type": "Bot"},
                ),
            },
        )
        self.assertEqual(
            decision.workflow, WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE
        )

    def test_oz_agent_assigned_to_ready_to_implement_routes_to_create_implementation(
        self,
    ) -> None:
        # Maintainer-driven assignment is the canonical way to kick
        # off implementation: oz-agent gets assigned, the
        # ``ready-to-implement`` label is already present, and the
        # webhook fires the create-implementation workflow.
        decision = route_event(
            "issues",
            {
                "action": "assigned",
                "assignee": {"login": OZ_AGENT_LOGIN},
                "issue": _issue(
                    labels=["triaged", "ready-to-implement"],
                    assignees=[OZ_AGENT_LOGIN],
                ),
            },
        )
        self.assertEqual(
            decision.workflow, WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE
        )

    def test_oz_agent_assigned_to_ready_to_spec_routes_to_create_spec(self) -> None:
        decision = route_event(
            "issues",
            {
                "action": "assigned",
                "assignee": {"login": OZ_AGENT_LOGIN},
                "issue": _issue(
                    labels=["triaged", "ready-to-spec"],
                    assignees=[OZ_AGENT_LOGIN],
                ),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_CREATE_SPEC_FROM_ISSUE)

    def test_oz_agent_self_assignment_from_app_bot_is_dropped(self) -> None:
        # The implementation workflow may best-effort assign
        # ``oz-agent`` while handling an issue-comment mention. GitHub
        # then emits a separate ``issues.assigned`` webhook authored by
        # the GitHub App bot; routing that event would start a second
        # implementation run for the same issue.
        decision = route_event(
            "issues",
            {
                "action": "assigned",
                "assignee": {"login": OZ_AGENT_LOGIN},
                "sender": {"login": "oz-for-oss[bot]", "type": "Bot"},
                "issue": _issue(
                    labels=["triaged", "ready-to-implement"],
                    assignees=[OZ_AGENT_LOGIN],
                ),
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("self-assignment", decision.reason)

    def test_oz_agent_self_assignment_from_oz_agent_sender_is_dropped(self) -> None:
        # GitHub issue timelines can surface the actor for the same
        # app-driven assignment as ``oz-agent``. Treat that as the same
        # self-assignment loop while preserving maintainer senders.
        decision = route_event(
            "issues",
            {
                "action": "assigned",
                "assignee": {"login": OZ_AGENT_LOGIN},
                "sender": {"login": OZ_AGENT_LOGIN, "type": "User"},
                "issue": _issue(
                    labels=["triaged", "ready-to-implement"],
                    assignees=[OZ_AGENT_LOGIN],
                ),
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("self-assignment", decision.reason)

    def test_assigned_ready_to_implement_takes_precedence_over_ready_to_spec(
        self,
    ) -> None:
        # An issue carrying both lifecycle labels at once (for
        # example, mid-promotion from spec to implementation) must
        # land on the implementation workflow so the bot does not
        # regenerate the spec.
        decision = route_event(
            "issues",
            {
                "action": "assigned",
                "assignee": {"login": OZ_AGENT_LOGIN},
                "issue": _issue(
                    labels=["triaged", "ready-to-spec", "ready-to-implement"],
                    assignees=[OZ_AGENT_LOGIN],
                ),
            },
        )
        self.assertEqual(
            decision.workflow, WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE
        )

    def test_issues_assigned_for_non_oz_agent_is_dropped(self) -> None:
        # Maintainers assigning a human use this event for their own
        # tracking; the bot must stay out of it even when the issue
        # carries a lifecycle label.
        decision = route_event(
            "issues",
            {
                "action": "assigned",
                "assignee": {"login": "alice"},
                "issue": _issue(
                    labels=["ready-to-implement"], assignees=["alice"]
                ),
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("non-oz-agent", decision.reason)

    def test_issues_assigned_without_lifecycle_label_is_dropped(self) -> None:
        decision = route_event(
            "issues",
            {
                "action": "assigned",
                "assignee": {"login": OZ_AGENT_LOGIN},
                "issue": _issue(
                    labels=["triaged"], assignees=[OZ_AGENT_LOGIN]
                ),
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("ready-to", decision.reason)

    def test_ready_to_implement_label_added_with_oz_agent_assignee_routes_to_create_implementation(
        self,
    ) -> None:
        decision = route_event(
            "issues",
            {
                "action": "labeled",
                "label": {"name": "ready-to-implement"},
                "issue": _issue(
                    labels=["triaged", "ready-to-implement"],
                    assignees=[OZ_AGENT_LOGIN],
                ),
            },
        )
        self.assertEqual(
            decision.workflow, WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE
        )

    def test_ready_to_spec_label_added_with_oz_agent_assignee_routes_to_create_spec(
        self,
    ) -> None:
        decision = route_event(
            "issues",
            {
                "action": "labeled",
                "label": {"name": "ready-to-spec"},
                "issue": _issue(
                    labels=["triaged", "ready-to-spec"],
                    assignees=[OZ_AGENT_LOGIN],
                ),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_CREATE_SPEC_FROM_ISSUE)

    def test_ready_to_spec_label_without_oz_agent_assignee_routes_to_announce(self) -> None:
        # Adding ``ready-to-spec`` without an ``oz-agent`` assignee
        # means the maintainer is opening the issue up for community
        # contribution rather than enlisting the bot. The webhook
        # routes that case to the announce-ready-issue sync handler
        # so contributors hear about it via a one-shot comment.
        decision = route_event(
            "issues",
            {
                "action": "labeled",
                "label": {"name": "ready-to-spec"},
                "issue": _issue(
                    labels=["triaged", "ready-to-spec"],
                    assignees=["alice"],
                ),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_ANNOUNCE_READY_ISSUE)
        self.assertIn("oz-agent", decision.reason)
        self.assertEqual(
            (decision.extra or {}).get("label"), "ready-to-spec"
        )

    def test_ready_to_implement_label_without_oz_agent_assignee_routes_to_announce(
        self,
    ) -> None:
        # Same routing as ``ready-to-spec``: the announce handler
        # fires whenever a lifecycle label lands without an
        # ``oz-agent`` assignee, regardless of which one.
        decision = route_event(
            "issues",
            {
                "action": "labeled",
                "label": {"name": "ready-to-implement"},
                "issue": _issue(
                    labels=["triaged", "ready-to-implement"],
                    assignees=[],
                ),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_ANNOUNCE_READY_ISSUE)
        self.assertEqual(
            (decision.extra or {}).get("label"), "ready-to-implement"
        )

    def test_unrelated_label_added_to_issue_is_dropped(self) -> None:
        decision = route_event(
            "issues",
            {
                "action": "labeled",
                "label": {"name": "good-first-issue"},
                "issue": _issue(
                    labels=["good-first-issue"], assignees=[OZ_AGENT_LOGIN]
                ),
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("unhandled label", decision.reason)

    def test_auto_implement_label_added_to_existing_issue_is_dropped(self) -> None:
        decision = route_event(
            "issues",
            {
                "action": "labeled",
                "label": {"name": AUTO_IMPLEMENT_LABEL},
                "issue": _issue(
                    labels=[AUTO_IMPLEMENT_LABEL], assignees=[OZ_AGENT_LOGIN]
                ),
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("unhandled label", decision.reason)

    def test_issues_edited_event_is_dropped(self) -> None:
        # ``edited`` and other actions outside of
        # ``opened``/``assigned``/``labeled`` should still fall
        # through to the catch-all so we do not silently miss
        # routing surface changes.
        decision = route_event(
            "issues",
            {
                "action": "edited",
                "issue": _issue(
                    labels=["ready-to-implement"], assignees=[OZ_AGENT_LOGIN]
                ),
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("not handled", decision.reason)


class IssueCommentEventTest(unittest.TestCase):
    def test_bot_comment_skipped(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(pull_request={"url": "..."}, labels=["triaged"]),
                "comment": _comment(body="@oz-agent help", login="dependabot[bot]", user_type="Bot"),
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("automation", decision.reason)

    def test_oz_review_command_on_pr_routes_to_review(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(pull_request={"url": "..."}),
                "comment": _comment(body="/oz-review please"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_REVIEW_PR)

    def test_oz_agent_review_alias_on_pr_routes_to_review(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(pull_request={"url": "..."}),
                "comment": _comment(body="please @oz-agent /review"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_REVIEW_PR)

    def test_oz_review_prefix_without_word_boundary_is_dropped(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(pull_request={"url": "..."}),
                "comment": _comment(body="/oz-reviewed this already"),
            },
        )
        self.assertIsNone(decision.workflow)

    def test_oz_verify_command_takes_precedence_over_review(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(pull_request={"url": "..."}),
                "comment": _comment(body="/oz-verify and also /oz-review"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_VERIFY_PR_COMMENT)

    def test_mention_on_pr_routes_to_respond_to_pr_comment(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(pull_request={"url": "..."}),
                "comment": _comment(body="hey @oz-agent can you take another look"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_RESPOND_TO_PR_COMMENT)

    def test_warp_agent_alias_on_pr_routes_to_respond_to_pr_comment(self) -> None:
        # The legacy ``@warp-agent`` handle is kept as an alias after the
        # rebrand so mentions written before the change still trigger a run.
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(pull_request={"url": "..."}),
                "comment": _comment(body="@warp-agent please fix clippy/lint issues"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_RESPOND_TO_PR_COMMENT)

    def test_warp_agent_review_alias_on_pr_routes_to_review(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(pull_request={"url": "..."}),
                "comment": _comment(body="please @warp-agent /review"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_REVIEW_PR)

    def test_unrecognized_agent_handle_on_pr_routes_to_acknowledge(self) -> None:
        # An agent-like but unrecognized handle (e.g. the pre-rebrand
        # ``@warp-bot`` typo) should surface a clarifying acknowledgement
        # instead of being silently dropped.
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(pull_request={"url": "..."}),
                "comment": _comment(body="@warp-bot please fix lint"),
            },
        )
        self.assertEqual(
            decision.workflow, WORKFLOW_ACKNOWLEDGE_UNKNOWN_MENTION
        )
        self.assertEqual(
            (decision.extra or {}).get("mentioned_handle"), "warp-bot"
        )

    def test_pr_comment_without_command_or_mention_skipped(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(pull_request={"url": "..."}),
                "comment": _comment(body="thanks for the feedback"),
            },
        )
        self.assertIsNone(decision.workflow)

    def test_unrelated_mention_on_pr_is_not_acknowledged(self) -> None:
        # A non-agent mention (a normal teammate) must not trigger an
        # acknowledgement; only agent-like handles do.
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(pull_request={"url": "..."}),
                "comment": _comment(body="cc @alice can you take a look"),
            },
        )
        self.assertIsNone(decision.workflow)

    def test_oz_agent_mention_on_triaged_plain_issue_routes_to_triage(self) -> None:
        # Mentioning the bot on a triaged issue should re-trigger triage
        # so any new context in the conversation is incorporated; this
        # closes the lifecycle gap where triaged issues with new
        # follow-up context should get another pass.
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(labels=["triaged"]),
                "comment": _comment(body="@oz-agent thoughts?"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_TRIAGE_NEW_ISSUES)

    def test_oz_agent_mention_on_ready_to_implement_issue_routes_to_create_implementation(self) -> None:
        # ``ready-to-implement`` issues already cleared triage; a
        # ``@oz-agent`` mention there should kick off the
        # implementation workflow rather than another triage.
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(labels=["triaged", "ready-to-implement"]),
                "comment": _comment(body="@oz-agent please re-evaluate"),
            },
        )
        self.assertEqual(
            decision.workflow, WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE
        )

    def test_oz_agent_mention_on_ready_to_spec_issue_routes_to_create_spec(self) -> None:
        # ``ready-to-spec`` issues already cleared triage; a
        # ``@oz-agent`` mention there should kick off the spec
        # workflow.
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(labels=["triaged", "ready-to-spec"]),
                "comment": _comment(body="@oz-agent please draft the spec"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_CREATE_SPEC_FROM_ISSUE)

    def test_ready_to_implement_takes_precedence_over_ready_to_spec(self) -> None:
        # An issue that somehow carries both labels (for example,
        # because a maintainer added ``ready-to-implement`` while
        # ``ready-to-spec`` was still attached) should land on the
        # implementation workflow so the bot does not regenerate the
        # spec.
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(
                    labels=["triaged", "ready-to-spec", "ready-to-implement"]
                ),
                "comment": _comment(body="@oz-agent go"),
            },
        )
        self.assertEqual(
            decision.workflow, WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE
        )

    def test_oz_agent_mention_on_non_triaged_plain_issue_routes_to_triage(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(labels=[]),
                "comment": _comment(body="@oz-agent please look"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_TRIAGE_NEW_ISSUES)

    def test_warp_agent_alias_on_plain_issue_routes_to_triage(self) -> None:
        # The legacy alias works on plain issues too.
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(labels=[]),
                "comment": _comment(body="@warp-agent please look"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_TRIAGE_NEW_ISSUES)

    def test_needs_info_reply_from_issue_author_routes_to_triage(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(
                    labels=["needs-info"],
                    user={"login": "alice", "type": "User"},
                ),
                "comment": _comment(body="Here's the version info", login="alice"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_TRIAGE_NEW_ISSUES)

    def test_needs_info_reply_from_other_user_is_dropped(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(
                    labels=["needs-info"],
                    user={"login": "alice", "type": "User"},
                ),
                "comment": _comment(body="Drive-by suggestion", login="bob"),
            },
        )
        self.assertIsNone(decision.workflow)

    def test_plain_issue_without_mention_or_needs_info_is_dropped(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(),
                "comment": _comment(body="thanks for filing this"),
            },
        )
        self.assertIsNone(decision.workflow)

    def test_edited_plain_issue_comment_with_oz_agent_mention_is_dropped(self) -> None:
        # Edited comments can arrive while a run triggered by the
        # previous comment body is still in flight. Do not dispatch a
        # second workflow for the same comment edit.
        decision = route_event(
            "issue_comment",
            {
                "action": "edited",
                "issue": _issue(labels=["triaged"]),
                "comment": _comment(body="@oz-agent updated context"),
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("not handled", decision.reason)

    def test_edited_pr_issue_comment_with_oz_agent_mention_is_dropped(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "edited",
                "issue": _issue(pull_request={"url": "..."}),
                "comment": _comment(body="@oz-agent updated PR context"),
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("not handled", decision.reason)

    def test_unhandled_action_skipped(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "deleted",
                "issue": _issue(pull_request={"url": "..."}),
                "comment": _comment(body="..."),
            },
        )
        self.assertIsNone(decision.workflow)


class PullRequestEventTest(unittest.TestCase):
    def test_opened_non_draft_pr_routes_to_review(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "opened",
                "pull_request": {"state": "open", "draft": False},
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_REVIEW_PR)

    def test_reopened_non_draft_pr_routes_to_review(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "reopened",
                "pull_request": {"state": "open", "draft": False},
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_REVIEW_PR)

    def test_opened_draft_pr_skipped(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "opened",
                "pull_request": {"state": "open", "draft": True},
            },
        )
        self.assertIsNone(decision.workflow)

    def test_review_requested_from_oz_routes_to_review(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "review_requested",
                "pull_request": {"state": "open"},
                "requested_reviewer": {"login": OZ_AGENT_LOGIN},
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_REVIEW_PR)

    def test_review_requested_from_other_user_skipped(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "review_requested",
                "pull_request": {"state": "open"},
                "requested_reviewer": {"login": "alice"},
            },
        )
        self.assertIsNone(decision.workflow)

    def test_oz_review_label_routes_to_review(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "labeled",
                "pull_request": {"state": "open"},
                "label": {"name": "oz-review"},
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_REVIEW_PR)

    def test_plan_approved_label_routes_to_plan_approved(self) -> None:
        # ``plan-approved`` is added by maintainers reviewing spec
        # PRs; routing the event to the dedicated workflow lets the
        # webhook handler fan out the spec-approved comment, the
        # ``ready-to-spec`` label removal, and the implementation
        # dispatch from a single ingress point.
        decision = route_event(
            "pull_request",
            {
                "action": "labeled",
                "pull_request": {"state": "open"},
                "label": {"name": "plan-approved"},
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_PLAN_APPROVED)

    def test_unrelated_pr_label_is_dropped(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "labeled",
                "pull_request": {"state": "open"},
                "label": {"name": "good-first-issue"},
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("unhandled label", decision.reason)

    def test_plan_approved_on_closed_pr_is_dropped(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "labeled",
                "pull_request": {"state": "closed"},
                "label": {"name": "plan-approved"},
            },
        )
        self.assertIsNone(decision.workflow)

    def test_synchronize_non_draft_pr_is_dropped(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "synchronize",
                "pull_request": {"state": "open", "draft": False},
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("not handled", decision.reason)

    def test_synchronize_draft_pr_is_dropped(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "synchronize",
                "pull_request": {"state": "open", "draft": True},
            },
        )
        self.assertIsNone(decision.workflow)

    def test_edited_is_dropped(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "edited",
                "pull_request": {"state": "open"},
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("not handled", decision.reason)

    def test_closed_pr_skipped(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "opened",
                "pull_request": {"state": "closed"},
            },
        )
        self.assertIsNone(decision.workflow)


class PullRequestReviewCommentTest(unittest.TestCase):
    def test_oz_review_command_routes_to_review(self) -> None:
        decision = route_event(
            "pull_request_review_comment",
            {
                "action": "created",
                "comment": _comment(body="/oz-review"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_REVIEW_PR)

    def test_oz_agent_review_alias_on_review_comment_routes_to_review(self) -> None:
        decision = route_event(
            "pull_request_review_comment",
            {
                "action": "created",
                "comment": _comment(body="please @oz-agent /review"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_REVIEW_PR)

    def test_mention_routes_to_respond_to_pr_comment(self) -> None:
        decision = route_event(
            "pull_request_review_comment",
            {
                "action": "created",
                "comment": _comment(body="@oz-agent address this"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_RESPOND_TO_PR_COMMENT)

    def test_warp_agent_alias_routes_to_respond_to_pr_comment(self) -> None:
        decision = route_event(
            "pull_request_review_comment",
            {
                "action": "created",
                "comment": _comment(body="@warp-agent address this"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_RESPOND_TO_PR_COMMENT)

    def test_unrecognized_handle_routes_to_acknowledge(self) -> None:
        decision = route_event(
            "pull_request_review_comment",
            {
                "action": "created",
                "comment": _comment(body="@ozagent address this"),
            },
        )
        self.assertEqual(
            decision.workflow, WORKFLOW_ACKNOWLEDGE_UNKNOWN_MENTION
        )
        self.assertEqual(
            (decision.extra or {}).get("mentioned_handle"), "ozagent"
        )

    def test_no_command_or_mention_skipped(self) -> None:
        decision = route_event(
            "pull_request_review_comment",
            {
                "action": "created",
                "comment": _comment(body="LGTM"),
            },
        )
        self.assertIsNone(decision.workflow)

    def test_bot_review_comment_skipped(self) -> None:
        decision = route_event(
            "pull_request_review_comment",
            {
                "action": "created",
                "comment": _comment(body="@oz-agent", login="oz-agent[bot]", user_type="Bot"),
            },
        )
        self.assertIsNone(decision.workflow)


class PullRequestReviewTest(unittest.TestCase):
    def test_mention_in_review_body_routes_to_respond_to_pr_comment(self) -> None:
        decision = route_event(
            "pull_request_review",
            {
                "action": "submitted",
                "review": _comment(body="@oz-agent please update this"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_RESPOND_TO_PR_COMMENT)

    def test_edited_review_body_mention_routes_to_respond_to_pr_comment(self) -> None:
        decision = route_event(
            "pull_request_review",
            {
                "action": "edited",
                "review": _comment(body="Follow-up for @oz-agent"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_RESPOND_TO_PR_COMMENT)

    def test_warp_agent_alias_in_review_body_routes_to_respond_to_pr_comment(self) -> None:
        decision = route_event(
            "pull_request_review",
            {
                "action": "submitted",
                "review": _comment(body="@warp-agent please update this"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_RESPOND_TO_PR_COMMENT)

    def test_unrecognized_handle_in_review_body_routes_to_acknowledge(self) -> None:
        decision = route_event(
            "pull_request_review",
            {
                "action": "submitted",
                "review": _comment(body="@oz_agent please update this"),
            },
        )
        self.assertEqual(
            decision.workflow, WORKFLOW_ACKNOWLEDGE_UNKNOWN_MENTION
        )
        self.assertEqual(
            (decision.extra or {}).get("mentioned_handle"), "oz_agent"
        )

    def test_review_body_without_mention_is_dropped(self) -> None:
        decision = route_event(
            "pull_request_review",
            {
                "action": "submitted",
                "review": _comment(body="LGTM"),
            },
        )
        self.assertIsNone(decision.workflow)

    def test_bot_review_body_is_dropped(self) -> None:
        decision = route_event(
            "pull_request_review",
            {
                "action": "submitted",
                "review": _comment(
                    body="@oz-agent", login="oz-agent[bot]", user_type="Bot"
                ),
            },
        )
        self.assertIsNone(decision.workflow)

    def test_unhandled_review_action_is_dropped(self) -> None:
        decision = route_event(
            "pull_request_review",
            {
                "action": "dismissed",
                "review": _comment(body="@oz-agent"),
            },
        )
        self.assertIsNone(decision.workflow)


class UnknownEventTest(unittest.TestCase):
    def test_unknown_event_returns_skip(self) -> None:
        decision = route_event("ping", {"zen": "Approachable is better than simple."})
        self.assertIsNone(decision.workflow)

    def test_non_object_payload_returns_skip(self) -> None:
        decision = route_event("issues", "not an object")  # type: ignore[arg-type]
        self.assertIsNone(decision.workflow)


class RouteDecisionDefaultsTest(unittest.TestCase):
    def test_decision_can_carry_extra_metadata(self) -> None:
        # Smoke test: callers occasionally attach extra metadata for
        # logging. The dataclass must accept it without breaking.
        decision = RouteDecision(workflow=None, reason="skip", extra={"trigger": "labeled"})
        self.assertEqual(decision.extra, {"trigger": "labeled"})


class AgentMentionHelpersTest(unittest.TestCase):
    """Unit coverage for the mention-detection helpers."""

    def test_recognized_handles(self) -> None:
        self.assertTrue(has_agent_mention("ping @oz-agent here"))
        self.assertTrue(has_agent_mention("ping @warp-agent here"))
        self.assertTrue(has_agent_mention("(@OZ-Agent)"))  # case-insensitive + punctuation

    def test_non_agent_mentions_are_not_recognized(self) -> None:
        self.assertFalse(has_agent_mention("cc @alice"))
        self.assertFalse(has_agent_mention("email me at foo@oz-agent.com"))
        self.assertFalse(has_agent_mention("no mentions at all"))
        # A longer handle that merely starts with the recognized text.
        self.assertFalse(has_agent_mention("@oz-agentx hello"))

    def test_unrecognized_agent_like_handles(self) -> None:
        self.assertEqual(find_unrecognized_agent_mention("@warp-bot go"), "warp-bot")
        self.assertEqual(find_unrecognized_agent_mention("@ozagent go"), "ozagent")
        self.assertEqual(find_unrecognized_agent_mention("@oz_agent go"), "oz_agent")
        self.assertEqual(find_unrecognized_agent_mention("@warp-ai go"), "warp-ai")

    def test_recognized_handles_are_not_flagged_as_unrecognized(self) -> None:
        self.assertIsNone(find_unrecognized_agent_mention("@oz-agent go"))
        self.assertIsNone(find_unrecognized_agent_mention("@warp-agent go"))

    def test_unrelated_mentions_are_not_flagged(self) -> None:
        self.assertIsNone(find_unrecognized_agent_mention("cc @alice"))
        self.assertIsNone(find_unrecognized_agent_mention("@warp please"))  # bare product name
        self.assertIsNone(find_unrecognized_agent_mention("@deploy-agent"))  # unrelated -agent user


if __name__ == "__main__":
    unittest.main()
