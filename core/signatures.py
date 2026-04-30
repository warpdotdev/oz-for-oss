"""GitHub webhook signature verification.

GitHub signs every webhook delivery with the shared secret configured on
the GitHub App. The signature is sent in the ``X-Hub-Signature-256``
header as ``sha256=<hex>``. We compute the HMAC-SHA256 of the raw
request body using the same secret and compare it in constant time.
"""

from __future__ import annotations

import hashlib
import hmac

# Header name GitHub uses for SHA-256 signed deliveries. The legacy
# ``X-Hub-Signature`` (SHA-1) is intentionally not supported here:
# GitHub strongly recommends preferring SHA-256 and we don't want to
# accept weaker signatures in a fresh implementation.
SIGNATURE_HEADER = "x-hub-signature-256"
_SIGNATURE_PREFIX = "sha256="


class SignatureVerificationError(Exception):
    """Raised when a webhook signature cannot be verified."""


def expected_signature(secret: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` signature GitHub would send for *body*.

    Exposed so tests and local-dev tooling can produce matching
    signatures without re-implementing the HMAC step.
    """
    if secret is None:
        raise ValueError("secret must be a non-empty string")
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not secret_bytes:
        raise ValueError("secret must be a non-empty string")
    digest = hmac.new(secret_bytes, body, hashlib.sha256).hexdigest()
    return f"{_SIGNATURE_PREFIX}{digest}"


def verify_signature(*, secret: str, body: bytes, signature_header: str | None) -> None:
    """Raise ``SignatureVerificationError`` when *signature_header* is invalid.

    The check is deliberately strict: a missing or malformed header,
    a truncated digest, or any signature/secret mismatch all surface
    as the same exception so the webhook handler can return a 401
    without leaking which check failed.
    """
    if not isinstance(signature_header, str) or not signature_header:
        raise SignatureVerificationError("missing signature header")
    header = signature_header.strip()
    if not header.startswith(_SIGNATURE_PREFIX):
        raise SignatureVerificationError("unexpected signature scheme")
    expected = expected_signature(secret, body)
    if not hmac.compare_digest(expected, header):
        raise SignatureVerificationError("signature mismatch")


def is_signature_valid(*, secret: str, body: bytes, signature_header: str | None) -> bool:
    """Return whether *signature_header* matches *body* under *secret*.

    Convenience wrapper around :func:`verify_signature` that swallows
    the structured exception. Prefer :func:`verify_signature` when the
    caller wants the failure reason in logs.
    """
    try:
        verify_signature(secret=secret, body=body, signature_header=signature_header)
    except SignatureVerificationError:
        return False
    return True


__all__ = [
    "SIGNATURE_HEADER",
    "SignatureVerificationError",
    "expected_signature",
    "is_signature_valid",
    "verify_signature",
]
