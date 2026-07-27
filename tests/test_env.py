"""Tests for ``oz.env`` environment helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from . import conftest  # noqa: F401

from oz.env import flag_env


class FlagEnvTest(unittest.TestCase):
    def test_unset_variable_returns_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIs(flag_env("WARP_COMPUTER_USE_ENABLED"), False)
            self.assertIs(flag_env("WARP_COMPUTER_USE_ENABLED", default=True), True)

    def test_empty_variable_returns_default(self) -> None:
        with patch.dict(os.environ, {"WARP_COMPUTER_USE_ENABLED": ""}, clear=True):
            self.assertIs(flag_env("WARP_COMPUTER_USE_ENABLED"), False)
            self.assertIs(flag_env("WARP_COMPUTER_USE_ENABLED", default=True), True)

    def test_truthy_values_enable(self) -> None:
        for value in ("1", "true", "TRUE", "True"):
            with patch.dict(os.environ, {"WARP_COMPUTER_USE_ENABLED": value}, clear=True):
                self.assertIs(
                    flag_env("WARP_COMPUTER_USE_ENABLED"), True, msg=f"value={value!r}"
                )

    def test_falsy_and_unrecognized_values_keep_default(self) -> None:
        for value in ("0", "false", "yes", "on", "garbage", "maybe"):
            with patch.dict(os.environ, {"WARP_COMPUTER_USE_ENABLED": value}, clear=True):
                self.assertIs(
                    flag_env("WARP_COMPUTER_USE_ENABLED"), False, msg=f"value={value!r}"
                )

    def test_trims_surrounding_whitespace(self) -> None:
        with patch.dict(os.environ, {"WARP_COMPUTER_USE_ENABLED": "  true  "}, clear=True):
            self.assertIs(flag_env("WARP_COMPUTER_USE_ENABLED"), True)


if __name__ == "__main__":
    unittest.main()
