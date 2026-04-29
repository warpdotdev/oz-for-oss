"""Tests for ``lib.routing``.

The webhook router owns every issue-driven and PR-driven Oz workflow
that the legacy ``.github/workflows/`` adapters used to host. These
tests cover the routes the webhook actually delivers and confirm that
out-of-band variants (non-Oz assignees, mismatched labels, etc.) are
dropped with a descriptive reason rather than dispatched anyway.
"""

from __future__ import annotations

import unittest

from . import conftest  # noqa: F401

from lib.routing import (
    OZ_AGENT_LOGIN,
    RouteDecision,
    WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
    WORKFLOW_CREATE_SPEC_FROM_ISSUE,
    WORKFLOW_ENFORCE_PR_ISSUE_STATE,
    WORKFLOW_RESPOND_TO_PR_COMMENT,
    WORKFLOW_REVIEW_PR,
    WORKFLOW_TRIAGE_NEW_ISSUES,
    WORKFLOW_VERIFY_PR_COMMENT,
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

    def test_lifecycle_label_added_without_oz_agent_assignee_is_dropped(self) -> None:
        # Adding ``ready-to-spec`` while only humans are assigned
        # must not fire the bot — the maintainer is staging the
        # label without delegating to oz-agent yet.
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
        self.assertIsNone(decision.workflow)
        self.assertIn("oz-agent", decision.reason)

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

    def test_oz_agent_mention_on_triaged_plain_issue_routes_to_triage(self) -> None:
        # Mentioning the bot on a triaged issue should re-trigger triage
        # so any new context in the conversation is incorporated; this
        # closes the lifecycle gap where issues with
        # ``ready-to-implement`` would otherwise fall through both the
        # webhook and the legacy ``respond-to-triaged-issue-comment``
        # workflow (which excludes ``ready-to-implement``).
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

    def test_synchronize_routes_to_enforce(self) -> None:
        decision = route_event(
            "pull_request",
            {
                "action": "synchronize",
                "pull_request": {"state": "open"},
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_ENFORCE_PR_ISSUE_STATE)

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

    def test_mention_routes_to_respond_to_pr_comment(self) -> None:
        decision = route_event(
            "pull_request_review_comment",
            {
                "action": "created",
                "comment": _comment(body="@oz-agent address this"),
            },
        )
        self.assertEqual(decision.workflow, WORKFLOW_RESPOND_TO_PR_COMMENT)

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


if __name__ == "__main__":
    unittest.main()
