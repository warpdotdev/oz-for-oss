from __future__ import annotations

import importlib.util
from pathlib import Path

from . import conftest  # noqa: F401


def _load_fetch_github_context():
    script_path = (
        Path(__file__).resolve().parent.parent
        / ".agents"
        / "skills"
        / "implement-specs"
        / "scripts"
        / "fetch_github_context.py"
    )
    spec = importlib.util.spec_from_file_location("fetch_github_context", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _user(login: str) -> dict[str, str]:
    return {"login": login}


def test_run_issue_includes_comments_with_author_association_metadata(monkeypatch) -> None:
    module = _load_fetch_github_context()
    monkeypatch.setattr(
        module,
        "_fetch_issue",
        lambda *args, **kwargs: {
            "number": 42,
            "title": "Bug report",
            "body": "Issue body",
            "author_association": "MEMBER",
            "user": _user("maintainer"),
        },
    )
    monkeypatch.setattr(
        module,
        "_fetch_issue_comments",
        lambda *args, **kwargs: [
            {
                "id": 1001,
                "body": "Contributor follow-up",
                "author_association": "CONTRIBUTOR",
                "created_at": "2026-04-30T20:00:00Z",
                "user": _user("external-contributor"),
            }
        ],
    )

    output = module.run_issue(
        "acme",
        "widgets",
        42,
        token="token",
        include_comments=True,
    )

    assert "Contributor follow-up" in output
    assert "kind=issue-comment" in output
    assert "author=@external-contributor" in output
    assert "association=CONTRIBUTOR" in output
    assert "[kind=issue-body | author=@maintainer | association=MEMBER | trust=TRUSTED | number=#42" in output
    assert "[kind=issue-comment | author=@external-contributor | association=CONTRIBUTOR | id=1001" in output
    assert "trust=UNTRUSTED" not in output


def test_run_pr_includes_conversation_review_comments_and_review_bodies_with_metadata(
    monkeypatch,
) -> None:
    module = _load_fetch_github_context()
    monkeypatch.setattr(
        module,
        "_fetch_pull",
        lambda *args, **kwargs: {
            "number": 7,
            "title": "Feature PR",
            "body": "PR body",
            "author_association": "MEMBER",
            "user": _user("maintainer"),
            "head": {"ref": "feature"},
            "base": {"ref": "main"},
        },
    )
    monkeypatch.setattr(
        module,
        "_fetch_issue_comments",
        lambda *args, **kwargs: [
            {
                "id": 2000,
                "body": "PR conversation from collaborator",
                "author_association": "COLLABORATOR",
                "created_at": "2026-04-30T20:00:30Z",
                "user": _user("collaborator"),
            },
            {
                "id": 2001,
                "body": "PR conversation from contributor",
                "author_association": "FIRST_TIME_CONTRIBUTOR",
                "created_at": "2026-04-30T20:01:00Z",
                "user": _user("first-timer"),
            }
        ],
    )
    monkeypatch.setattr(
        module,
        "_fetch_pr_review_comments",
        lambda *args, **kwargs: [
            {
                "id": 3001,
                "body": "Inline review from contributor",
                "author_association": "NONE",
                "created_at": "2026-04-30T20:02:00Z",
                "path": "src/main.py",
                "line": 12,
                "user": _user("drive-by-reviewer"),
            }
        ],
    )
    monkeypatch.setattr(
        module,
        "_fetch_pr_reviews",
        lambda *args, **kwargs: [
            {
                "id": 4001,
                "body": "Review body from contributor",
                "author_association": "CONTRIBUTOR",
                "state": "COMMENTED",
                "submitted_at": "2026-04-30T20:03:00Z",
                "user": _user("review-body-author"),
            }
        ],
    )

    output = module.run_pr(
        "acme",
        "widgets",
        7,
        token="token",
        include_comments=True,
        include_diff=False,
    )

    assert "PR conversation from contributor" in output
    assert "PR conversation from collaborator" in output
    assert "[kind=pr-body | author=@maintainer | association=MEMBER | trust=TRUSTED | number=#7" in output
    assert "[kind=pr-issue-comment | author=@collaborator | association=COLLABORATOR | trust=TRUSTED | id=2000" in output
    assert "kind=pr-issue-comment" in output
    assert "author=@first-timer" in output
    assert "association=FIRST_TIME_CONTRIBUTOR" in output
    assert "[kind=pr-issue-comment | author=@first-timer | association=FIRST_TIME_CONTRIBUTOR | id=2001" in output
    assert "Inline review from contributor" in output
    assert "kind=pr-review-comment" in output
    assert "author=@drive-by-reviewer" in output
    assert "association=NONE" in output
    assert "[kind=pr-review-comment | author=@drive-by-reviewer | association=NONE | id=3001" in output
    assert "Review body from contributor" in output
    assert "kind=pr-review" in output
    assert "author=@review-body-author" in output
    assert "association=CONTRIBUTOR" in output
    assert "[kind=pr-review | author=@review-body-author | association=CONTRIBUTOR | id=4001" in output
    assert "trust=UNTRUSTED" not in output
