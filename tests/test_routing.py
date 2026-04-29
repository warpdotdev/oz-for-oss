"""Tests for ``lib.routing``.

The webhook router only handles PR-driven events; issue-triggered and
plan-approval traffic stays on the GitHub Actions workflows under
``.github/workflows/``. These tests cover the routes the webhook
actually owns and confirm that issue-only payloads are dropped with a
descriptive reason rather than re-dispatched.
"""

from __future__ import annotations

import unittest

from . import conftest  # noqa: F401

from lib.routing import (
    OZ_AGENT_LOGIN,
    RouteDecision,
    WORKFLOW_ENFORCE_PR_ISSUE_STATE,
    WORKFLOW_RESPOND_TO_PR_COMMENT,
    WORKFLOW_REVIEW_PR,
    WORKFLOW_VERIFY_PR_COMMENT,
    route_event,
)


def _issue(*, labels=None, assignees=None, pull_request=None):
    return {
        "number": 42,
        "labels": [{"name": label} for label in labels or []],
        "assignees": [{"login": login} for login in assignees or []],
        **({"pull_request": pull_request} if pull_request else {}),
    }


def _comment(*, body, login="alice", user_type="User"):
    return {
        "id": 1,
        "body": body,
        "user": {"login": login, "type": user_type},
        "author_association": "MEMBER",
    }


class IssuesEventNotRoutedTest(unittest.TestCase):
    """``issues`` events are owned by GitHub Actions and never routed."""

    def test_issues_event_is_dropped(self) -> None:
        decision = route_event("issues", {"action": "opened", "issue": _issue()})
        self.assertIsNone(decision.workflow)
        self.assertIn("not handled", decision.reason)

    def test_issues_assigned_event_is_dropped(self) -> None:
        decision = route_event(
            "issues",
            {
                "action": "assigned",
                "assignee": {"login": OZ_AGENT_LOGIN},
                "issue": _issue(labels=["ready-to-implement"], assignees=[OZ_AGENT_LOGIN]),
            },
        )
        self.assertIsNone(decision.workflow)


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

    def test_plain_issue_comment_is_dropped_for_github_actions(self) -> None:
        decision = route_event(
            "issue_comment",
            {
                "action": "created",
                "issue": _issue(labels=["triaged"]),
                "comment": _comment(body="@oz-agent thoughts?"),
            },
        )
        self.assertIsNone(decision.workflow)
        self.assertIn("GitHub Actions", decision.reason)

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
