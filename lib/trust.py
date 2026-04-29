"""Trust evaluation for incoming webhook actors.

The control plane must decide whether the human attached to a webhook
event is allowed to drive an Oz workflow. The rules mirror the
``check_trust`` job in ``.github/workflows/respond-to-triaged-issue-comment.yml``
and the org-membership probe used by ``oz_workflows.helpers.is_trusted_commenter``:

- Author associations of ``OWNER``, ``MEMBER``, or ``COLLABORATOR`` are
  always trusted.
- Otherwise, probe ``GET /orgs/{org}/members/{login}``: a 204 means the
  user is an org member with private membership, so they are trusted.
- Any other status (404, 302, network error) leaves the actor untrusted
  so we fail closed.
"""

from __future__ import annotations

from typing import Any, Protocol


ORG_MEMBER_ASSOCIATIONS = {"COLLABORATOR", "MEMBER", "OWNER"}


class OrgMembershipProbe(Protocol):
    """Callable that probes ``/orgs/{org}/members/{login}`` and returns the status code.

    Implemented by :func:`probe_org_membership` against a real GitHub
    App installation token. Tests inject a stub.
    """

    def __call__(self, *, org: str, login: str) -> int: ...


def is_org_member_association(association: Any) -> bool:
    """Return whether *association* indicates direct org membership."""
    if not isinstance(association, str):
        return False
    return association.strip().upper() in ORG_MEMBER_ASSOCIATIONS


def evaluate_actor_trust(
    *,
    actor: dict[str, Any] | None,
    org: str,
    probe: OrgMembershipProbe,
) -> bool:
    """Decide whether *actor* should be trusted to drive a workflow.

    *actor* is a webhook ``comment``/``review`` sub-object containing
    ``user.login`` and ``author_association``. Returns ``True`` when
    the author association already proves membership, or when the org
    membership probe returns 204. Any other outcome — missing actor,
    missing login, association unknown, probe failure — falls back to
    untrusted.
    """
    if not isinstance(actor, dict):
        return False
    if is_org_member_association(actor.get("author_association")):
        return True
    user = actor.get("user")
    login = ""
    if isinstance(user, dict):
        login = str(user.get("login") or "").strip()
    if not login or not org:
        return False
    try:
        status = probe(org=org, login=login)
    except Exception:
        return False
    return status == 204


__all__ = [
    "ORG_MEMBER_ASSOCIATIONS",
    "OrgMembershipProbe",
    "evaluate_actor_trust",
    "is_org_member_association",
]
