from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from . import conftest  # noqa: F401

from api.cron import _resolve_cron_secret


class CronSecretTest(unittest.TestCase):
    def test_returns_configured_cron_secret(self) -> None:
        with patch.dict(os.environ, {"CRON_SECRET": "secret"}, clear=True):
            self.assertEqual(_resolve_cron_secret(), "secret")

    def test_missing_cron_secret_fails_closed_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "CRON_SECRET is required"):
                _resolve_cron_secret()

    def test_local_opt_out_allows_missing_cron_secret(self) -> None:
        with patch.dict(
            os.environ,
            {"OZ_ALLOW_UNAUTHENTICATED_CRON": "true"},
            clear=True,
        ):
            self.assertIsNone(_resolve_cron_secret())


if __name__ == "__main__":
    unittest.main()
