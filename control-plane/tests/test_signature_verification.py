"""Tests for ``control_plane.lib.signatures``."""

from __future__ import annotations

import unittest

# Importing ``conftest`` forces ``sys.path`` to include the control-plane root,
# which is required when pytest's auto-discovery is bypassed (for example, when
# the suite is run with ``python -m unittest discover``).
from . import conftest  # noqa: F401

from lib.signatures import (
    SIGNATURE_HEADER,
    SignatureVerificationError,
    expected_signature,
    is_signature_valid,
    verify_signature,
)


# A representative GitHub-style webhook body. The exact contents do not
# matter — the test only needs deterministic bytes for HMAC.
_BODY = b'{"action":"opened","issue":{"number":42}}'
_SECRET = "my-shared-secret"


class ExpectedSignatureTest(unittest.TestCase):
    def test_returns_sha256_prefixed_hex_digest(self) -> None:
        sig = expected_signature(_SECRET, _BODY)
        self.assertTrue(sig.startswith("sha256="))
        # The hex digest length for SHA-256 is 64 chars.
        self.assertEqual(len(sig), len("sha256=") + 64)

    def test_changes_when_body_changes(self) -> None:
        sig_a = expected_signature(_SECRET, _BODY)
        sig_b = expected_signature(_SECRET, _BODY + b" ")
        self.assertNotEqual(sig_a, sig_b)

    def test_changes_when_secret_changes(self) -> None:
        sig_a = expected_signature(_SECRET, _BODY)
        sig_b = expected_signature(_SECRET + "!", _BODY)
        self.assertNotEqual(sig_a, sig_b)

    def test_rejects_empty_secret(self) -> None:
        with self.assertRaises(ValueError):
            expected_signature("", _BODY)

    def test_rejects_none_secret(self) -> None:
        with self.assertRaises(ValueError):
            expected_signature(None, _BODY)  # type: ignore[arg-type]


class VerifySignatureTest(unittest.TestCase):
    def test_accepts_valid_signature(self) -> None:
        signature = expected_signature(_SECRET, _BODY)
        # Must not raise.
        verify_signature(secret=_SECRET, body=_BODY, signature_header=signature)

    def test_rejects_missing_header(self) -> None:
        with self.assertRaises(SignatureVerificationError):
            verify_signature(secret=_SECRET, body=_BODY, signature_header=None)

    def test_rejects_empty_header(self) -> None:
        with self.assertRaises(SignatureVerificationError):
            verify_signature(secret=_SECRET, body=_BODY, signature_header="")

    def test_rejects_unprefixed_header(self) -> None:
        with self.assertRaises(SignatureVerificationError):
            verify_signature(secret=_SECRET, body=_BODY, signature_header="abc123")

    def test_rejects_sha1_header(self) -> None:
        # Even if the SHA-1 hex matches, we never accept the legacy
        # SHA-1 envelope.
        with self.assertRaises(SignatureVerificationError):
            verify_signature(
                secret=_SECRET,
                body=_BODY,
                signature_header="sha1=abc",
            )

    def test_rejects_truncated_signature(self) -> None:
        signature = expected_signature(_SECRET, _BODY)[:-2]
        with self.assertRaises(SignatureVerificationError):
            verify_signature(secret=_SECRET, body=_BODY, signature_header=signature)

    def test_rejects_signature_for_different_body(self) -> None:
        signature = expected_signature(_SECRET, _BODY + b" ")
        with self.assertRaises(SignatureVerificationError):
            verify_signature(secret=_SECRET, body=_BODY, signature_header=signature)

    def test_rejects_signature_with_wrong_secret(self) -> None:
        signature = expected_signature("other-secret", _BODY)
        with self.assertRaises(SignatureVerificationError):
            verify_signature(secret=_SECRET, body=_BODY, signature_header=signature)

    def test_strips_surrounding_whitespace(self) -> None:
        signature = expected_signature(_SECRET, _BODY)
        # GitHub never emits whitespace, but be permissive when the
        # payload travels through proxies.
        verify_signature(
            secret=_SECRET,
            body=_BODY,
            signature_header=f" {signature} \t",
        )


class IsSignatureValidTest(unittest.TestCase):
    def test_returns_true_for_valid_signature(self) -> None:
        signature = expected_signature(_SECRET, _BODY)
        self.assertTrue(is_signature_valid(secret=_SECRET, body=_BODY, signature_header=signature))

    def test_returns_false_for_invalid_signature(self) -> None:
        self.assertFalse(
            is_signature_valid(secret=_SECRET, body=_BODY, signature_header="sha256=deadbeef")
        )

    def test_returns_false_when_header_missing(self) -> None:
        self.assertFalse(is_signature_valid(secret=_SECRET, body=_BODY, signature_header=None))


class HeaderConstantTest(unittest.TestCase):
    def test_header_is_lowercased(self) -> None:
        # Vercel's BaseHTTPRequestHandler.headers is case-insensitive,
        # but downstream consumers (logs, audit trails) compare against
        # a constant. Lock the constant to lowercase so the assertion
        # is unambiguous regardless of platform.
        self.assertEqual(SIGNATURE_HEADER, "x-hub-signature-256")


if __name__ == "__main__":
    unittest.main()
