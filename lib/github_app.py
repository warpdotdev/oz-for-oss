"""Mint GitHub App installation tokens for the control plane.

The Vercel webhook handler and cron poller authenticate to GitHub via a
GitHub App. This module covers the two-step token exchange:

1. Sign a short-lived JWT using the App's private key + ``app_id``.
2. POST ``/app/installations/{installation_id}/access_tokens`` to get a
   per-installation token.

The exchange is a tiny amount of code and avoids pulling another HTTP
client into the runtime.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import jwt


# JWT lifetime for the App-level token. GitHub requires < 10 minutes;
# 9 minutes leaves headroom for clock skew.
_JWT_LIFETIME_SECONDS = 9 * 60
# Buffer subtracted from `iat` to tolerate small clock skew on the
# Vercel runtime relative to GitHub.
_JWT_CLOCK_SKEW_SECONDS = 60


@dataclass(frozen=True)
class InstallationToken:
    """Per-installation token returned by the access-tokens endpoint."""

    token: str
    expires_at: str


class HttpClient(Protocol):
    """Minimal HTTP surface used to call the access-tokens endpoint."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Any: ...


def build_app_jwt(*, app_id: str, private_key: str, now: float | None = None) -> str:
    """Mint a short-lived JWT for the GitHub App.

    *now* is parameterized for tests; in production this is wall-clock
    time. The JWT uses RS256 because that is the only algorithm GitHub
    accepts for App authentication.
    """
    if not app_id:
        raise ValueError("app_id must be a non-empty string")
    if not private_key:
        raise ValueError("private_key must be a non-empty string")
    issued_at = int((now if now is not None else time.time()) - _JWT_CLOCK_SKEW_SECONDS)
    payload = {
        "iat": issued_at,
        "exp": issued_at + _JWT_LIFETIME_SECONDS,
        "iss": str(app_id),
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def fetch_installation_token(
    *,
    installation_id: int,
    app_id: str,
    private_key: str,
    http: HttpClient,
    api_base: str = "https://api.github.com",
    now: float | None = None,
) -> InstallationToken:
    """Exchange the App JWT for a per-installation access token."""
    if installation_id <= 0:
        raise ValueError("installation_id must be a positive integer")
    app_token = build_app_jwt(app_id=app_id, private_key=private_key, now=now)
    response = http.post(
        f"{api_base}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    )
    status = getattr(response, "status_code", 0)
    if status not in (200, 201):
        body = getattr(response, "text", "")
        raise RuntimeError(
            f"GitHub access-tokens endpoint returned {status}: {body}"
        )
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("access-tokens endpoint returned a non-object body")
    token = str(data.get("token") or "")
    expires_at = str(data.get("expires_at") or "")
    if not token:
        raise RuntimeError("access-tokens endpoint returned an empty token")
    return InstallationToken(token=token, expires_at=expires_at)


__all__ = [
    "HttpClient",
    "InstallationToken",
    "build_app_jwt",
    "fetch_installation_token",
]
