from __future__ import annotations
from contextlib import closing
from typing import Any, Mapping

from github import Auth, Github

from oz_workflows.env import load_event, repo_parts, repo_slug, require_env
from oz_workflows.helpers import WorkflowProgressComment


DEFAULT_ASSIGNEE_LOGIN = "oz-agent"


def resolve_assignee_login(event: Mapping[str, Any]) -> str:
    """Return the assignee login from a webhook payload, defaulting to oz-agent.

    Guards against both a missing ``assignee`` key and an explicit ``null``
    value, which GitHub sends on unassignment events. Using ``or {}`` (rather
    than the default argument to ``dict.get``) ensures we don't attempt to call
    ``.get`` on ``None``.
    """
    return (event.get("assignee") or {}).get("login") or DEFAULT_ASSIGNEE_LOGIN


def main() -> None:
    owner, repo = repo_parts()
    event = load_event()
    issue_number = int(event["issue"]["number"])
    assignee_login = resolve_assignee_login(event)
    with closing(Github(auth=Auth.Token(require_env("GH_TOKEN")))) as client:
        github = client.get_repo(repo_slug())
        issue = github.get_issue(issue_number)
        progress = WorkflowProgressComment(
            github,
            owner,
            repo,
            issue_number,
            workflow="comment-on-unready-assigned-issue",
            event_payload=event,
        )
        progress.start("I'm checking whether this assignment is ready for work.")
        progress.complete(
            "This issue is assigned to me, but it is not labeled `ready-to-spec` or `ready-to-implement`, so there is no work to do yet.",
        )
        issue.remove_from_assignees(assignee_login)


if __name__ == "__main__":
    main()
