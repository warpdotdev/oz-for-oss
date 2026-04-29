from __future__ import annotations

import unittest

from types import SimpleNamespace

from oz_workflows.helpers import (
    _MANUAL_LINKED_ISSUES_QUERY,
    _summarize_commits,
    all_review_comments_text,
    build_next_steps_section,
    build_spec_preview_section,
    build_pr_body,
    coauthor_prompt_lines,
    conventional_commit_prefix,
    find_matching_spec_prs,
    is_automation_user,
    is_spec_only_pr,
    is_trusted_commenter,
    format_issue_comments_for_prompt,
    POWERED_BY_SUFFIX,
    resolve_coauthor_line,
    resolve_pr_association,
    resolve_issue_number_for_pr,
    resolve_progress_requester_login,
    review_thread_comments_text,
    triggering_comment_prompt_text,
)


class BuildSpecPreviewSectionTest(unittest.TestCase):
    def test_builds_markdown_links_for_spec_branch(self) -> None:
        result = build_spec_preview_section("warpdotdev", "oz-oss-testbed", "oz-agent/spec-issue-20", 20)
        self.assertIn("Preview generated specs:", result)
        self.assertIn("[specs/GH20/product.md](https://github.com/warpdotdev/oz-oss-testbed/blob/oz-agent/spec-issue-20/specs/GH20/product.md)", result)
        self.assertIn("[specs/GH20/tech.md](https://github.com/warpdotdev/oz-oss-testbed/blob/oz-agent/spec-issue-20/specs/GH20/tech.md)", result)

class GraphQLQueryShapeTest(unittest.TestCase):
    def test_manual_linked_issues_query_has_balanced_braces(self) -> None:
        self.assertEqual(
            _MANUAL_LINKED_ISSUES_QUERY.count("{"),
            _MANUAL_LINKED_ISSUES_QUERY.count("}"),
        )


class BuildNextStepsSectionTest(unittest.TestCase):
    def test_builds_bulleted_next_steps(self) -> None:
        self.assertEqual(
            build_next_steps_section(
                [
                    "Review the plan PR.",
                    "Request any needed updates.",
                ]
            ),
            "Next steps:\n- Review the plan PR.\n- Request any needed updates.",
        )


class TriggeringCommentPromptTextTest(unittest.TestCase):
    def test_formats_comment_body_for_prompt(self) -> None:
        self.assertEqual(
            triggering_comment_prompt_text(
                {
                    "sender": {"login": "alice"},
                    "comment": {
                        "body": "@oz-agent please focus on rollout safety",
                        "user": {"login": "alice"},
                    },
                }
            ),
            "@alice commented:\n@oz-agent please focus on rollout safety",
        )

class IsAutomationUserTest(unittest.TestCase):
    def test_detects_github_bot_user_type(self) -> None:
        self.assertTrue(is_automation_user({"login": "some-user", "type": "Bot"}))

    def test_detects_bot_suffix_login(self) -> None:
        self.assertTrue(is_automation_user({"login": "dependabot[bot]", "type": "User"}))

    def test_returns_false_for_human_user(self) -> None:
        self.assertFalse(is_automation_user({"login": "alice", "type": "User"}))

    def test_returns_false_for_none_user(self) -> None:
        self.assertFalse(is_automation_user(None))


class IsTrustedCommenterTest(unittest.TestCase):
    """Trust evaluation for the ``respond-to-pr-comment`` gate.

    The trust check must produce a deterministic yes/no BEFORE the agent
    runs so the agent does not have to infer trust from the presence of
    the triggering comment in ``fetch_github_context.py`` output. That
    inference was the root cause of issue #311: a missing fetch-script
    output looked identical to "commenter was filtered as untrusted",
    so any unrelated fetch failure caused the agent to silently no-op
    on legitimate org-member comments.
    """

    def _client_with_org_membership(
        self, *, status: int, raise_exception: bool = False
    ) -> object:
        """Build a fake PyGithub client whose requester returns *status*.

        ``is_trusted_commenter`` calls ``client.requester.requestJson``
        directly (PyGithub exposes the requester as a public property),
        so the fake only needs that single method.
        """

        calls: list[tuple[str, str]] = []

        def _request_json(verb: str, path: str):
            calls.append((verb, path))
            if raise_exception:
                raise RuntimeError("boom")
            return status, {}, ""

        requester = SimpleNamespace(requestJson=_request_json)
        client = SimpleNamespace(requester=requester, calls=calls)
        return client

    def test_static_allowlist_short_circuits_without_api_call(self) -> None:
        """``OWNER``/``MEMBER``/``COLLABORATOR`` authors skip the org probe."""
        for association in ("OWNER", "MEMBER", "COLLABORATOR"):
            with self.subTest(association=association):
                client = self._client_with_org_membership(status=204)
                event = {
                    "comment": {
                        "author_association": association,
                        "user": {"login": "alice"},
                    }
                }
                self.assertTrue(
                    is_trusted_commenter(client, event, org="acme")
                )
                # Static allowlist must NOT make an API call.
                self.assertEqual(client.calls, [])

    def test_contributor_promoted_via_org_membership_probe(self) -> None:
        """Private org members still report as ``CONTRIBUTOR`` on event payloads.

        GitHub's ``author_association`` field is scoped to the repository,
        so an org member whose membership is private is reported as
        ``CONTRIBUTOR`` on the PR/comment event. We must fall back to
        ``GET /orgs/{org}/members/{login}``; a 204 response promotes the
        author to trusted.
        """
        client = self._client_with_org_membership(status=204)
        event = {
            "comment": {
                "author_association": "CONTRIBUTOR",
                "user": {"login": "private-member"},
            }
        }
        self.assertTrue(is_trusted_commenter(client, event, org="acme"))
        self.assertEqual(
            client.calls, [("GET", "/orgs/acme/members/private-member")]
        )

    def test_non_member_falls_through_and_is_untrusted(self) -> None:
        """A 404 on the org-membership probe keeps the author untrusted."""
        client = self._client_with_org_membership(status=404)
        event = {
            "comment": {
                "author_association": "NONE",
                "user": {"login": "outsider"},
            }
        }
        self.assertFalse(is_trusted_commenter(client, event, org="acme"))

    def test_probe_redirect_is_untrusted(self) -> None:
        """A 302 redirect (caller can't see private membership) is untrusted."""
        client = self._client_with_org_membership(status=302)
        event = {
            "comment": {
                "author_association": "CONTRIBUTOR",
                "user": {"login": "someone"},
            }
        }
        self.assertFalse(is_trusted_commenter(client, event, org="acme"))

    def test_fails_closed_when_probe_raises(self) -> None:
        """Exceptions during the probe must NOT grant trust."""
        client = self._client_with_org_membership(
            status=204, raise_exception=True
        )
        event = {
            "comment": {
                "author_association": "CONTRIBUTOR",
                "user": {"login": "someone"},
            }
        }
        self.assertFalse(is_trusted_commenter(client, event, org="acme"))

    def test_missing_comment_is_untrusted(self) -> None:
        """Events without a comment block cannot be evaluated as trusted."""
        self.assertFalse(
            is_trusted_commenter(
                self._client_with_org_membership(status=204),
                {"sender": {"login": "alice"}},
                org="acme",
            )
        )

    def test_missing_login_is_untrusted(self) -> None:
        """Skip the probe (and fail closed) when we have no login to probe for."""
        client = self._client_with_org_membership(status=204)
        event = {
            "comment": {
                "author_association": "CONTRIBUTOR",
                "user": {"login": ""},
            }
        }
        self.assertFalse(is_trusted_commenter(client, event, org="acme"))
        self.assertEqual(client.calls, [])

    def test_url_encodes_org_and_login(self) -> None:
        """Unusual org/login characters must be URL-encoded in the probe path."""
        client = self._client_with_org_membership(status=204)
        event = {
            "comment": {
                "author_association": "CONTRIBUTOR",
                "user": {"login": "weird user"},
            }
        }
        is_trusted_commenter(client, event, org="acme corp")
        self.assertEqual(
            client.calls, [("GET", "/orgs/acme%20corp/members/weird%20user")]
        )


class ResolveProgressRequesterLoginTest(unittest.TestCase):
    def test_fallback_precedence(self) -> None:
        """Explicit > comment author > sender for requester resolution."""
        cases = [
            (
                "explicit_requester_login",
                {"requester_login": "@alice"},
                "alice",
            ),
            (
                "comment_author_wins_over_sender",
                {
                    "event_payload": {
                        "sender": {"login": "bob"},
                        "comment": {"user": {"login": "alice"}},
                    },
                },
                "alice",
            ),
            (
                "sender_fallback",
                {"event_payload": {"sender": {"login": "bob"}}},
                "bob",
            ),
        ]
        for label, kwargs, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(
                    resolve_progress_requester_login(
                        FakeGitHubClient(), "acme", "widgets", 12, **kwargs
                    ),
                    expected,
                )


class FormatIssueCommentsForPromptTest(unittest.TestCase):
    def test_includes_non_org_member_comments(self) -> None:
        self.assertEqual(
            format_issue_comments_for_prompt(
                [
                    {
                        "id": 1,
                        "author_association": "NONE",
                        "created_at": "2026-03-24T00:00:00Z",
                        "body": "External contributor context",
                        "user": {"login": "contributor", "type": "User"},
                    },
                ],
                metadata_prefix="<!-- oz-agent-metadata:",
            ),
            "- @contributor [NONE] (2026-03-24T00:00:00Z): External contributor context",
        )

    def test_can_exclude_triggering_comment(self) -> None:
        self.assertEqual(
            format_issue_comments_for_prompt(
                [
                    {
                        "id": 1,
                        "author_association": "MEMBER",
                        "created_at": "2026-03-24T00:00:00Z",
                        "body": "Earlier context",
                        "user": {"login": "alice", "type": "User"},
                    },
                    {
                        "id": 2,
                        "author_association": "MEMBER",
                        "created_at": "2026-03-24T01:00:00Z",
                        "body": "@oz-agent please handle this",
                        "user": {"login": "alice", "type": "User"},
                    },
                ],
                metadata_prefix="<!-- oz-agent-metadata:",
                exclude_comment_id=2,
            ),
            "- @alice [MEMBER] (2026-03-24T00:00:00Z): Earlier context",
        )

    def test_filters_automation_authored_comments(self) -> None:
        self.assertEqual(
            format_issue_comments_for_prompt(
                [
                    {
                        "id": 1,
                        "author_association": "MEMBER",
                        "created_at": "2026-03-24T00:00:00Z",
                        "body": "Oz metadata comment",
                        "user": {"login": "oz-agent[bot]", "type": "Bot"},
                    },
                ],
                metadata_prefix="<!-- oz-agent-metadata:",
            ),
            "- None",
        )

class ConventionalCommitPrefixTest(unittest.TestCase):
    def test_label_to_prefix_table(self) -> None:
        cases = [
            (
                "bug_label_returns_fix",
                [{"name": "bug"}, {"name": "ready-to-implement"}],
                "fix",
            ),
            ("enhancement", [{"name": "enhancement"}], "feat"),
            ("feature", [{"name": "feature"}], "feat"),
            ("documentation", [{"name": "documentation"}], "docs"),
            (
                "no_matching_label_returns_default",
                [{"name": "ready-to-implement"}, {"name": "area/workflows"}],
                "feat",
            ),
            ("empty_labels", [], "feat"),
            ("string_labels", ["bug", "urgent"], "fix"),
        ]
        for label, labels, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(conventional_commit_prefix(labels), expected)

    def test_custom_default(self) -> None:
        self.assertEqual(conventional_commit_prefix([], default="chore"), "chore")

    def test_case_insensitive(self) -> None:
        self.assertEqual(conventional_commit_prefix([{"name": "Bug"}]), "fix")

    def test_first_match_wins(self) -> None:
        labels = [{"name": "bug"}, {"name": "enhancement"}]
        self.assertEqual(conventional_commit_prefix(labels), "fix")


class ReviewThreadCommentsTextTest(unittest.TestCase):
    def test_extracts_thread_for_trigger_comment(self) -> None:
        comments = [
            {"id": 10, "author_association": "MEMBER", "user": {"login": "alice"}, "created_at": "2026-01-01T00:00:00Z", "body": "Root comment", "path": "src/main.py"},
            {"id": 11, "in_reply_to_id": 10, "author_association": "MEMBER", "user": {"login": "bob"}, "created_at": "2026-01-01T01:00:00Z", "body": "Reply in thread", "path": "src/main.py"},
            {"id": 20, "author_association": "MEMBER", "user": {"login": "carol"}, "created_at": "2026-01-01T02:00:00Z", "body": "Different thread", "path": "src/other.py"},
        ]
        result = review_thread_comments_text(comments, trigger_comment_id=11)
        self.assertIn("alice", result)
        self.assertIn("bob", result)
        self.assertNotIn("carol", result)

    def test_includes_comment_id_prefix(self) -> None:
        comments = [
            {"id": 10, "author_association": "MEMBER", "user": {"login": "alice"}, "created_at": "2026-01-01T00:00:00Z", "body": "Root comment", "path": "src/main.py"},
            {"id": 11, "in_reply_to_id": 10, "author_association": "MEMBER", "user": {"login": "bob"}, "created_at": "2026-01-01T01:00:00Z", "body": "Reply", "path": "src/main.py"},
        ]
        result = review_thread_comments_text(comments, trigger_comment_id=11)
        self.assertIn("[id=10]", result)
        self.assertIn("[id=11]", result)

    def test_filters_non_org_members(self) -> None:
        comments = [
            {"id": 10, "author_association": "MEMBER", "user": {"login": "alice"}, "created_at": "2026-01-01T00:00:00Z", "body": "Root", "path": "f.py"},
            {"id": 11, "in_reply_to_id": 10, "author_association": "NONE", "user": {"login": "outsider"}, "created_at": "2026-01-01T01:00:00Z", "body": "External reply", "path": "f.py"},
        ]
        result = review_thread_comments_text(comments, trigger_comment_id=10)
        self.assertIn("alice", result)
        self.assertNotIn("outsider", result)

    def test_returns_empty_when_no_org_members(self) -> None:
        comments = [
            {"id": 10, "author_association": "NONE", "user": {"login": "outsider"}, "created_at": "2026-01-01T00:00:00Z", "body": "Comment", "path": "f.py"},
        ]
        self.assertEqual(review_thread_comments_text(comments, trigger_comment_id=10), "")


class AllReviewCommentsTextTest(unittest.TestCase):
    def test_groups_by_file_path(self) -> None:
        comments = [
            {"id": 1, "author_association": "MEMBER", "user": {"login": "alice"}, "created_at": "2026-01-01T00:00:00Z", "body": "Fix here", "path": "src/a.py"},
            {"id": 2, "author_association": "MEMBER", "user": {"login": "bob"}, "created_at": "2026-01-01T01:00:00Z", "body": "And here", "path": "src/b.py"},
        ]
        result = all_review_comments_text(comments)
        self.assertIn("File: src/a.py", result)
        self.assertIn("File: src/b.py", result)
        self.assertIn("alice", result)
        self.assertIn("bob", result)

    def test_includes_comment_id_prefix(self) -> None:
        comments = [
            {"id": 1, "author_association": "MEMBER", "user": {"login": "alice"}, "created_at": "2026-01-01T00:00:00Z", "body": "Fix here", "path": "src/a.py"},
            {"id": 2, "author_association": "MEMBER", "user": {"login": "bob"}, "created_at": "2026-01-01T01:00:00Z", "body": "And here", "path": "src/a.py"},
        ]
        result = all_review_comments_text(comments)
        self.assertIn("[id=1]", result)
        self.assertIn("[id=2]", result)

    def test_filters_non_org_members(self) -> None:
        comments = [
            {"id": 1, "author_association": "MEMBER", "user": {"login": "alice"}, "created_at": "2026-01-01T00:00:00Z", "body": "Good", "path": "f.py"},
            {"id": 2, "author_association": "NONE", "user": {"login": "outsider"}, "created_at": "2026-01-01T01:00:00Z", "body": "Bad", "path": "f.py"},
        ]
        result = all_review_comments_text(comments)
        self.assertIn("alice", result)
        self.assertNotIn("outsider", result)

    def test_returns_empty_when_no_org_members(self) -> None:
        comments = [
            {"id": 1, "author_association": "CONTRIBUTOR", "user": {"login": "ext"}, "created_at": "2026-01-01T00:00:00Z", "body": "Hi", "path": "f.py"},
        ]
        self.assertEqual(all_review_comments_text(comments), "")


class SummarizeCommitsTest(unittest.TestCase):
    def test_extracts_first_line_of_each_commit(self) -> None:
        commits = [
            {"commit": {"message": "Add feature X\n\nMore details here"}},
            {"commit": {"message": "Fix typo in docs"}},
        ]
        self.assertEqual(
            _summarize_commits(commits),
            "- Add feature X\n- Fix typo in docs",
        )

    def test_skips_merge_commits(self) -> None:
        commits = [
            {"commit": {"message": "Merge branch 'main' into feature"}},
            {"commit": {"message": "Real change"}},
        ]
        self.assertEqual(_summarize_commits(commits), "- Real change")

    def test_skips_empty_messages(self) -> None:
        commits = [
            {"commit": {"message": ""}},
            {"commit": {"message": "Valid commit"}},
        ]
        self.assertEqual(_summarize_commits(commits), "- Valid commit")

    def test_returns_empty_string_for_no_commits(self) -> None:
        self.assertEqual(_summarize_commits([]), "")


class BuildPrBodyTest(unittest.TestCase):
    def test_implementation_pr_includes_closing_keyword(self) -> None:
        github = FakeGitHubClientWithCompare([
            {"commit": {"message": "Implement the thing"}},
        ])
        body = build_pr_body(
            github, "acme", "widgets",
            issue_number=42,
            head="feature-branch",
            base="main",
            closing_keyword="Closes",
        )
        self.assertIn("Closes #42", body)
        self.assertIn("- Implement the thing", body)

    def test_plan_pr_omits_closing_keyword(self) -> None:
        github = FakeGitHubClientWithCompare([
            {"commit": {"message": "Add plan"}},
        ])
        body = build_pr_body(
            github, "acme", "widgets",
            issue_number=42,
            head="plan-branch",
            base="main",
            closing_keyword="",
        )
        self.assertNotIn("Closes", body)
        self.assertIn("Related issue: #42", body)
        self.assertIn("- Add plan", body)

    def test_includes_session_link(self) -> None:
        github = FakeGitHubClientWithCompare([])
        body = build_pr_body(
            github, "acme", "widgets",
            issue_number=7,
            head="branch",
            base="main",
            session_link="https://example.com/session",
            closing_keyword="Fixes",
        )
        self.assertIn("Session: [view on Warp](https://example.com/session)", body)

    def test_no_changes_section_when_no_commits(self) -> None:
        github = FakeGitHubClientWithCompare([])
        body = build_pr_body(
            github, "acme", "widgets",
            issue_number=7,
            head="branch",
            base="main",
            closing_keyword="Closes",
        )
        self.assertNotIn("## Changes", body)
        self.assertIn("Closes #7", body)

    def test_includes_powered_by_suffix(self) -> None:
        github = FakeGitHubClientWithCompare([])
        body = build_pr_body(
            github, "acme", "widgets",
            issue_number=7,
            head="branch",
            base="main",
            session_link="https://example.com/session",
            closing_keyword="Closes",
        )
        self.assertTrue(
            body.endswith(POWERED_BY_SUFFIX),
            msg=f"PR body should end with the suffix, got: {body!r}",
        )
        self.assertEqual(body.count(POWERED_BY_SUFFIX), 1)


class _FakeIssueWithEvents:
    def get_events(self) -> list[object]:
        return []


class FakeGitHubClient:
    """A minimal stand-in for ``github.Repository.Repository`` / ``github.Github``."""

    def __init__(self, *, users: dict[str, dict[str, object]] | None = None) -> None:
        self._users = users or {}

    def get_issue(self, issue_number: int) -> _FakeIssueWithEvents:
        return _FakeIssueWithEvents()

    def get_user(self, username: str) -> dict[str, object] | None:
        return self._users.get(username)


class ResolveCoauthorLineTest(unittest.TestCase):
    def test_resolves_old_format_for_pre_cutoff_account(self) -> None:
        github = FakeGitHubClient(users={"alice": {"name": "Alice Smith", "login": "alice", "id": 100, "created_at": "2015-01-01T00:00:00Z"}})
        event = {"comment": {"user": {"login": "alice"}}, "sender": {"login": "bot"}}
        self.assertEqual(
            resolve_coauthor_line(github, event),
            "Co-Authored-By: Alice Smith <alice@users.noreply.github.com>",
        )

    def test_resolves_new_format_for_post_cutoff_account(self) -> None:
        github = FakeGitHubClient(users={"alice": {"name": "Alice Smith", "login": "alice", "id": 12345678, "created_at": "2020-06-15T00:00:00Z"}})
        event = {"comment": {"user": {"login": "alice"}}, "sender": {"login": "bot"}}
        self.assertEqual(
            resolve_coauthor_line(github, event),
            "Co-Authored-By: Alice Smith <12345678+alice@users.noreply.github.com>",
        )

    def test_resolves_from_sender_when_no_comment(self) -> None:
        github = FakeGitHubClient(users={"bob": {"name": "Bob Jones", "login": "bob", "id": 200, "created_at": "2016-03-01T00:00:00Z"}})
        event = {"sender": {"login": "bob"}}
        self.assertEqual(
            resolve_coauthor_line(github, event),
            "Co-Authored-By: Bob Jones <bob@users.noreply.github.com>",
        )

    def test_falls_back_to_login_when_name_is_none(self) -> None:
        github = FakeGitHubClient(users={"alice": {"name": None, "login": "alice", "id": 99999, "created_at": "2023-01-01T00:00:00Z"}})
        event = {"comment": {"user": {"login": "alice"}}}
        self.assertEqual(
            resolve_coauthor_line(github, event),
            "Co-Authored-By: alice <99999+alice@users.noreply.github.com>",
        )

    def test_falls_back_to_login_when_get_user_returns_none(self) -> None:
        github = FakeGitHubClient(users={})
        event = {"sender": {"login": "unknown-user"}}
        self.assertEqual(
            resolve_coauthor_line(github, event),
            "Co-Authored-By: unknown-user <unknown-user@users.noreply.github.com>",
        )

    def test_returns_empty_when_no_login_available(self) -> None:
        github = FakeGitHubClient()
        event: dict[str, object] = {}
        self.assertEqual(resolve_coauthor_line(github, event), "")

    def test_handles_get_user_exception(self) -> None:
        class ErrorClient(FakeGitHubClient):
            def get_user(self, username: str) -> dict[str, object] | None:
                raise RuntimeError("API error")

        github = ErrorClient()
        event = {"sender": {"login": "alice"}}
        self.assertEqual(
            resolve_coauthor_line(github, event),
            "Co-Authored-By: alice <alice@users.noreply.github.com>",
        )

    def test_cutoff_boundary_uses_new_format(self) -> None:
        """An account created exactly on the cutoff date uses the ID+login format."""
        github = FakeGitHubClient(users={"edge": {"name": "Edge User", "login": "edge", "id": 55555, "created_at": "2017-07-18T00:00:00Z"}})
        event = {"sender": {"login": "edge"}}
        self.assertEqual(
            resolve_coauthor_line(github, event),
            "Co-Authored-By: Edge User <55555+edge@users.noreply.github.com>",
        )

    def test_old_format_when_no_id_in_profile(self) -> None:
        """Falls back to old format when user profile has no id."""
        github = FakeGitHubClient(users={"alice": {"name": "Alice", "login": "alice", "created_at": "2023-01-01T00:00:00Z"}})
        event = {"sender": {"login": "alice"}}
        self.assertEqual(
            resolve_coauthor_line(github, event),
            "Co-Authored-By: Alice <alice@users.noreply.github.com>",
        )

    def test_old_format_when_no_created_at_in_profile(self) -> None:
        """Falls back to old format when user profile has no created_at."""
        github = FakeGitHubClient(users={"alice": {"name": "Alice", "login": "alice", "id": 12345}})
        event = {"sender": {"login": "alice"}}
        self.assertEqual(
            resolve_coauthor_line(github, event),
            "Co-Authored-By: Alice <alice@users.noreply.github.com>",
        )


class CoauthorPromptLinesTest(unittest.TestCase):
    def test_returns_include_directive_when_line_provided(self) -> None:
        result = coauthor_prompt_lines("Co-Authored-By: Alice <alice@users.noreply.github.com>")
        self.assertIn("configure the local git author and committer as `Oz <oz-agent@warp.dev>`", result)
        self.assertIn('git config user.name "Oz"', result)
        self.assertIn("Do not derive the git author or committer", result)
        self.assertIn("Co-Authored-By: Alice <alice@users.noreply.github.com>", result)
        self.assertIn("Do not attempt to resolve the co-author identity yourself", result)
        self.assertIn("Do not include issue number references", result)

    def test_returns_omit_directive_when_empty(self) -> None:
        result = coauthor_prompt_lines("")
        self.assertIn("configure the local git author and committer as `Oz <oz-agent@warp.dev>`", result)
        self.assertIn('git config user.email "oz-agent@warp.dev"', result)
        self.assertIn("Do not include any Co-Authored-By lines", result)
        self.assertIn("Do not include issue number references", result)


class IsSpecOnlyPrTest(unittest.TestCase):
    def test_all_specs_files(self) -> None:
        self.assertTrue(is_spec_only_pr(["specs/GH42/product.md", "specs/GH42/tech.md"]))

    def test_single_spec_file(self) -> None:
        self.assertTrue(is_spec_only_pr(["specs/GH10/product.md"]))

    def test_mixed_files(self) -> None:
        self.assertFalse(is_spec_only_pr(["specs/GH42/product.md", "src/review_pr.py"]))

    def test_no_spec_files(self) -> None:
        self.assertFalse(is_spec_only_pr(["src/main.py", "README.md"]))

    def test_empty_file_list(self) -> None:
        self.assertFalse(is_spec_only_pr([]))


class _FakeComparison:
    def __init__(self, commits: list[dict[str, object]]) -> None:
        self.commits = commits


class _FakeGraphQLRequester:
    def __init__(
        self,
        *,
        closing_pages: list[dict[str, object]] | None = None,
        timeline_pages: list[dict[str, object]] | None = None,
    ) -> None:
        self._closing_pages = closing_pages or [
            {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        ]
        self._timeline_pages = timeline_pages or [
            {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        ]
        self._closing_index = 0
        self._timeline_index = 0

    def graphql_query(
        self, query: str, variables: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, object]]:
        if "closingIssuesReferences" in query:
            page = self._closing_pages[self._closing_index]
            self._closing_index += 1
            return (
                {},
                {"data": {"repository": {"pullRequest": {"closingIssuesReferences": page}}}},
            )
        if "timelineItems" in query:
            page = self._timeline_pages[self._timeline_index]
            self._timeline_index += 1
            return (
                {},
                {"data": {"repository": {"pullRequest": {"timelineItems": page}}}},
            )
        raise AssertionError(f"Unexpected GraphQL query: {query}")


class _FakeAssociationGithub:
    def __init__(
        self,
        *,
        issues: dict[int, SimpleNamespace] | None = None,
        requester: object | None = None,
    ) -> None:
        self._issues = issues or {}
        self.requester = requester
        self.seen: list[int] = []

    def get_issue(self, number: int) -> SimpleNamespace:
        self.seen.append(number)
        return self._issues[number]


def _fake_issue(number: int, *, pull_request: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(pull_request=pull_request, number=number)


class FakeGitHubClientWithCompare:
    """A minimal stand-in for ``github.Repository.Repository.compare``."""

    def __init__(self, commits: list[dict[str, object]]) -> None:
        self._commits = commits

    def compare(self, base: str, head: str) -> _FakeComparison:
        return _FakeComparison(self._commits)


class ResolveIssueNumberForPrTest(unittest.TestCase):
    def test_prefers_single_deterministic_issue_over_linked_issue_data(self) -> None:
        requester = _FakeGraphQLRequester(
            closing_pages=[
                {
                    "nodes": [
                        {
                            "number": 99,
                            "repository": {
                                "owner": {"login": "acme"},
                                "name": "widgets",
                            },
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            ]
        )
        github = _FakeAssociationGithub(
            issues={42: _fake_issue(42)},
            requester=requester,
        )
        pr = SimpleNamespace(
            number=7,
            head=SimpleNamespace(ref="oz-agent/implement-issue-42"),
            body="See #99",
        )

        association = resolve_pr_association(github, "acme", "widgets", pr, [])

        self.assertEqual(association["deterministic_issue_numbers"], [42])
        self.assertEqual(association["same_repo_issue_numbers"], [42, 99])
        self.assertEqual(association["primary_issue_number"], 42)
        self.assertFalse(association["ambiguous"])

    def test_ignores_pr_body_text_without_authoritative_link_data(self) -> None:
        github = _FakeAssociationGithub(requester=_FakeGraphQLRequester())
        pr = SimpleNamespace(
            number=7,
            head=SimpleNamespace(ref="feature-branch"),
            body="Addresses #42 and see #43",
        )

        association = resolve_pr_association(github, "acme", "widgets", pr, [])

        self.assertEqual(association["same_repo_issue_numbers"], [])
        self.assertIsNone(association["primary_issue_number"])
        self.assertFalse(association["ambiguous"])
        self.assertEqual(github.seen, [])

    def test_uses_same_repo_github_links_when_no_deterministic_signal_exists(self) -> None:
        requester = _FakeGraphQLRequester(
            closing_pages=[
                {
                    "nodes": [
                        {
                            "number": 77,
                            "repository": {
                                "owner": {"login": "acme"},
                                "name": "widgets",
                            },
                        },
                        {
                            "number": 88,
                            "repository": {
                                "owner": {"login": "other"},
                                "name": "repo",
                            },
                        },
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            ]
        )
        github = _FakeAssociationGithub(requester=requester)
        pr = SimpleNamespace(
            number=7,
            head=SimpleNamespace(ref="feature-branch"),
            body="Closes #77",
        )

        association = resolve_pr_association(github, "acme", "widgets", pr, [])

        self.assertEqual(association["same_repo_issue_numbers"], [77])
        self.assertEqual(association["primary_issue_number"], 77)
        self.assertEqual(
            [issue["number"] for issue in association["github_linked_issues"]],
            [77, 88],
        )

    def test_reconstructs_manual_links_after_disconnects(self) -> None:
        requester = _FakeGraphQLRequester(
            timeline_pages=[
                {
                    "nodes": [
                        {
                            "__typename": "ConnectedEvent",
                            "subject": {
                                "number": 10,
                                "repository": {
                                    "owner": {"login": "acme"},
                                    "name": "widgets",
                                },
                            },
                        },
                        {
                            "__typename": "ConnectedEvent",
                            "subject": {
                                "number": 11,
                                "repository": {
                                    "owner": {"login": "acme"},
                                    "name": "widgets",
                                },
                            },
                        },
                        {
                            "__typename": "DisconnectedEvent",
                            "subject": {
                                "number": 10,
                                "repository": {
                                    "owner": {"login": "acme"},
                                    "name": "widgets",
                                },
                            },
                        },
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            ]
        )
        github = _FakeAssociationGithub(requester=requester)
        pr = SimpleNamespace(
            number=7,
            head=SimpleNamespace(ref="feature-branch"),
            body="",
        )

        association = resolve_pr_association(github, "acme", "widgets", pr, [])

        self.assertEqual(association["same_repo_issue_numbers"], [11])
        self.assertEqual(association["primary_issue_number"], 11)
        self.assertEqual(
            [issue["source"] for issue in association["github_linked_issues"]],
            ["manualLink"],
        )

    def test_returns_no_primary_when_same_repo_links_are_ambiguous(self) -> None:
        requester = _FakeGraphQLRequester(
            closing_pages=[
                {
                    "nodes": [
                        {
                            "number": 10,
                            "repository": {
                                "owner": {"login": "acme"},
                                "name": "widgets",
                            },
                        },
                        {
                            "number": 11,
                            "repository": {
                                "owner": {"login": "acme"},
                                "name": "widgets",
                            },
                        },
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            ]
        )
        github = _FakeAssociationGithub(requester=requester)
        pr = SimpleNamespace(
            number=7,
            head=SimpleNamespace(ref="feature-branch"),
            body="",
        )

        association = resolve_pr_association(github, "acme", "widgets", pr, [])

        self.assertEqual(association["same_repo_issue_numbers"], [10, 11])
        self.assertIsNone(association["primary_issue_number"])
        self.assertTrue(association["ambiguous"])

    def test_uses_provided_issue_cache_to_avoid_duplicate_calls(self) -> None:
        github = _FakeAssociationGithub(issues={42: _fake_issue(42)})
        pr = SimpleNamespace(
            number=7,
            head=SimpleNamespace(ref="oz-agent/implement-issue-42"),
            body="",
        )
        cache: dict[int, object] = {}
        # First resolution populates the cache.
        first = resolve_issue_number_for_pr(
            github, "acme", "widgets", pr, [], issue_cache=cache
        )
        # Second resolution should reuse the cache and not issue another call.
        second = resolve_issue_number_for_pr(
            github, "acme", "widgets", pr, [], issue_cache=cache
        )
        self.assertEqual(first, 42)
        self.assertEqual(second, 42)
        self.assertEqual(github.seen, [42])
        self.assertIn(42, cache)

    def test_returns_none_when_no_candidates(self) -> None:
        pr = SimpleNamespace(number=7, head=SimpleNamespace(ref="main"), body="")
        self.assertIsNone(
            resolve_issue_number_for_pr(_FakeAssociationGithub(), "acme", "widgets", pr, [])
        )

    def test_skips_candidates_that_are_pull_requests(self) -> None:
        github = _FakeAssociationGithub(
            issues={
                100: _fake_issue(100, pull_request=object()),
                42: _fake_issue(42),
            }
        )
        pr = SimpleNamespace(
            number=7,
            head=SimpleNamespace(ref="oz-agent/spec-issue-100"),
            body="",
        )
        result = resolve_issue_number_for_pr(
            github, "acme", "widgets", pr, ["specs/GH42/product.md"]
        )
        self.assertEqual(result, 42)
        self.assertEqual(github.seen, [100, 42])


class FindMatchingSpecPrsTest(unittest.TestCase):
    def test_reads_labels_directly_without_calling_as_issue(self) -> None:
        pr_calls = {"as_issue": 0}

        class FakePR:
            def __init__(self, number: int, label_names: list[str]) -> None:
                self.number = number
                self.html_url = f"https://github.com/acme/widgets/pull/{number}"
                self.updated_at = "2026-04-01T00:00:00Z"
                self.head = SimpleNamespace(
                    ref="oz-agent/spec-issue-7",
                    repo=SimpleNamespace(full_name="acme/widgets"),
                )
                self.labels = [SimpleNamespace(name=name) for name in label_names]

            def as_issue(self) -> None:
                pr_calls["as_issue"] += 1
                raise AssertionError("find_matching_spec_prs should not call pr.as_issue()")

            def get_files(self) -> list[SimpleNamespace]:
                return [SimpleNamespace(filename="specs/GH7/product.md")]

        class FakeGitHub:
            def get_pulls(self, *, state: str, head: str) -> list[FakePR]:
                return [
                    FakePR(1, ["plan-approved"]),
                    FakePR(2, []),
                ]

        approved, unapproved = find_matching_spec_prs(FakeGitHub(), "acme", "widgets", 7)
        self.assertEqual([pr["number"] for pr in approved], [1])
        self.assertEqual([pr["number"] for pr in unapproved], [2])
        self.assertEqual(pr_calls["as_issue"], 0)


if __name__ == "__main__":
    unittest.main()
