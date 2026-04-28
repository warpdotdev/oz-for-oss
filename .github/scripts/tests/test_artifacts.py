from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import httpx

from oz_workflows.artifacts import (
    ISSUE_RESPONSE_FILENAME,
    PR_METADATA_FILENAME,
    REVIEW_FILENAME,
    TRIAGE_RESULT_FILENAME,
    _DOWNLOAD_MAX_ATTEMPTS,
    _download_artifact_text,
    _download_artifact_json,
    _find_file_artifact,
    load_issue_response_artifact,
    load_pr_metadata_artifact,
    load_review_artifact,
    load_run_artifact,
    load_triage_artifact,
    poll_for_artifact,
    poll_for_text_artifact,
    try_load_pr_metadata_artifact,
)


def _make_artifact(artifact_type: str, filename: str, artifact_uid: str) -> MagicMock:
    data = MagicMock()
    data.filename = filename
    data.artifact_uid = artifact_uid
    artifact = MagicMock()
    artifact.artifact_type = artifact_type
    artifact.data = data
    return artifact


class FindFileArtifactTest(unittest.TestCase):
    def test_finds_matching_file_artifact(self) -> None:
        run = MagicMock()
        run.artifacts = [
            _make_artifact("PLAN", "plan.md", "uid-plan"),
            _make_artifact("FILE", "review.json", "uid-review"),
        ]
        self.assertEqual(_find_file_artifact(run, "review.json"), "uid-review")

    def test_returns_none_when_artifact_not_found(self) -> None:
        """``_find_file_artifact`` returns ``None`` for every no-artifact case."""
        cases = [
            (
                "no_matching_filename",
                [_make_artifact("FILE", "other.json", "uid-other")],
            ),
            ("empty_artifact_list", []),
            ("artifacts_is_none", None),
            (
                "non_file_artifact_type",
                [_make_artifact("SCREENSHOT", "review.json", "uid-screenshot")],
            ),
        ]
        for label, artifacts in cases:
            with self.subTest(label=label):
                run = MagicMock()
                run.artifacts = artifacts
                self.assertIsNone(_find_file_artifact(run, "review.json"))


class DownloadArtifactJsonTest(unittest.TestCase):
    @patch("oz_workflows.artifacts.httpx.Client")
    def test_downloads_text(self, mock_client_cls: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "## Summary\n- Item"
        mock_response.raise_for_status = MagicMock()
        mock_http = MagicMock()
        mock_http.get.return_value = mock_response
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        client = MagicMock()
        artifact_response = MagicMock()
        artifact_response.data.download_url = "https://storage.example.com/signed-url"
        client.agent.get_artifact.return_value = artifact_response

        result = _download_artifact_text(client, "uid-123")
        self.assertEqual(result, "## Summary\n- Item")
        client.agent.get_artifact.assert_called_once_with("uid-123")
        mock_http.get.assert_called_once_with("https://storage.example.com/signed-url")

    @patch("oz_workflows.artifacts.httpx.Client")
    def test_downloads_and_parses_json(self, mock_client_cls: MagicMock) -> None:
        expected = {"summary": "looks good", "comments": []}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = json.dumps(expected)
        mock_response.raise_for_status = MagicMock()
        mock_http = MagicMock()
        mock_http.get.return_value = mock_response
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        client = MagicMock()
        artifact_response = MagicMock()
        artifact_response.data.download_url = "https://storage.example.com/signed-url"
        client.agent.get_artifact.return_value = artifact_response

        result = _download_artifact_json(client, "uid-123")
        self.assertEqual(result, expected)
        client.agent.get_artifact.assert_called_once_with("uid-123")
        mock_http.get.assert_called_once_with("https://storage.example.com/signed-url")

    def test_raises_when_no_download_url(self) -> None:
        client = MagicMock()
        artifact_response = MagicMock()
        artifact_response.data.download_url = None
        client.agent.get_artifact.return_value = artifact_response

        with self.assertRaises(RuntimeError):
            _download_artifact_json(client, "uid-123")


def _make_mock_http_client(get_side_effect: list) -> MagicMock:
    """Build a mock httpx.Client whose ``get`` replays *get_side_effect*."""
    mock_http = MagicMock()
    mock_http.get.side_effect = get_side_effect
    mock_http.__enter__ = MagicMock(return_value=mock_http)
    mock_http.__exit__ = MagicMock(return_value=False)
    return mock_http


def _make_http_response(status_code: int, text: str = "") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.request = MagicMock()
    if status_code >= 400:
        def _raise_for_status() -> None:
            raise httpx.HTTPStatusError(
                f"HTTP {status_code}", request=response.request, response=response
            )
        response.raise_for_status.side_effect = _raise_for_status
    else:
        response.raise_for_status = MagicMock()
    return response


class DownloadArtifactRetryTest(unittest.TestCase):
    """Tests for the retry behavior in ``_download_artifact_text``."""

    def _make_client(self) -> MagicMock:
        client = MagicMock()
        artifact_response = MagicMock()
        artifact_response.data.download_url = "https://storage.example.com/signed"
        client.agent.get_artifact.return_value = artifact_response
        return client

    @patch("oz_workflows.artifacts.time.sleep", return_value=None)
    @patch("oz_workflows.artifacts.httpx.Client")
    def test_retries_on_repeated_5xx_then_succeeds(
        self, mock_client_cls: MagicMock, _mock_sleep: MagicMock
    ) -> None:
        responses = [
            _make_http_response(503),
            _make_http_response(500),
            _make_http_response(200, "hello"),
        ]
        mock_http = _make_mock_http_client(responses)
        mock_client_cls.return_value = mock_http

        result = _download_artifact_text(self._make_client(), "uid-123")
        self.assertEqual(result, "hello")
        self.assertEqual(mock_http.get.call_count, 3)

    @patch("oz_workflows.artifacts.time.sleep", return_value=None)
    @patch("oz_workflows.artifacts.httpx.Client")
    def test_retries_on_network_exceptions_then_succeeds(
        self, mock_client_cls: MagicMock, _mock_sleep: MagicMock
    ) -> None:
        request = httpx.Request("GET", "https://storage.example.com/signed")
        responses = [
            httpx.ConnectError("boom", request=request),
            httpx.ReadTimeout("slow", request=request),
            _make_http_response(200, "body"),
        ]
        mock_http = _make_mock_http_client(responses)
        mock_client_cls.return_value = mock_http

        result = _download_artifact_text(self._make_client(), "uid-123")
        self.assertEqual(result, "body")
        self.assertEqual(mock_http.get.call_count, 3)

    @patch("oz_workflows.artifacts.time.sleep", return_value=None)
    @patch("oz_workflows.artifacts.httpx.Client")
    def test_raises_after_max_attempts_on_5xx(
        self, mock_client_cls: MagicMock, _mock_sleep: MagicMock
    ) -> None:
        responses = [_make_http_response(500) for _ in range(_DOWNLOAD_MAX_ATTEMPTS)]
        mock_http = _make_mock_http_client(responses)
        mock_client_cls.return_value = mock_http

        with self.assertRaises(httpx.HTTPStatusError):
            _download_artifact_text(self._make_client(), "uid-123")
        self.assertEqual(mock_http.get.call_count, _DOWNLOAD_MAX_ATTEMPTS)

    @patch("oz_workflows.artifacts.time.sleep", return_value=None)
    @patch("oz_workflows.artifacts.httpx.Client")
    def test_raises_after_max_attempts_on_network_errors(
        self, mock_client_cls: MagicMock, _mock_sleep: MagicMock
    ) -> None:
        request = httpx.Request("GET", "https://storage.example.com/signed")
        responses = [
            httpx.ConnectError(f"boom-{i}", request=request)
            for i in range(_DOWNLOAD_MAX_ATTEMPTS)
        ]
        mock_http = _make_mock_http_client(responses)
        mock_client_cls.return_value = mock_http

        with self.assertRaises(httpx.ConnectError):
            _download_artifact_text(self._make_client(), "uid-123")
        self.assertEqual(mock_http.get.call_count, _DOWNLOAD_MAX_ATTEMPTS)

    @patch("oz_workflows.artifacts.time.sleep", return_value=None)
    @patch("oz_workflows.artifacts.httpx.Client")
    def test_does_not_retry_on_4xx(
        self, mock_client_cls: MagicMock, _mock_sleep: MagicMock
    ) -> None:
        responses = [_make_http_response(404)]
        mock_http = _make_mock_http_client(responses)
        mock_client_cls.return_value = mock_http

        with self.assertRaises(httpx.HTTPStatusError):
            _download_artifact_text(self._make_client(), "uid-123")
        self.assertEqual(mock_http.get.call_count, 1)

    @patch("oz_workflows.artifacts.time.sleep", return_value=None)
    @patch("oz_workflows.artifacts.httpx.Client")
    def test_mixed_transient_failures_then_success(
        self, mock_client_cls: MagicMock, _mock_sleep: MagicMock
    ) -> None:
        request = httpx.Request("GET", "https://storage.example.com/signed")
        responses = [
            _make_http_response(502),
            httpx.ReadError("short read", request=request),
            _make_http_response(503),
            _make_http_response(200, "ok"),
        ]
        mock_http = _make_mock_http_client(responses)
        mock_client_cls.return_value = mock_http

        result = _download_artifact_text(self._make_client(), "uid-abc")
        self.assertEqual(result, "ok")
        self.assertEqual(mock_http.get.call_count, 4)


class PollForArtifactTest(unittest.TestCase):
    @patch("oz_workflows.artifacts.build_oz_client")
    @patch("oz_workflows.artifacts._download_artifact_json")
    def test_returns_immediately_when_artifact_present(
        self, mock_download: MagicMock, mock_build_client: MagicMock
    ) -> None:
        expected = {"hello": "world"}
        mock_download.return_value = expected

        run = MagicMock()
        run.artifacts = [_make_artifact("FILE", "review.json", "uid-abc")]
        client = MagicMock()
        client.agent.runs.retrieve.return_value = run
        mock_build_client.return_value = client

        result = poll_for_artifact("run-123", filename="review.json", timeout_seconds=0)
        self.assertEqual(result, expected)

    @patch("oz_workflows.artifacts.build_oz_client")
    @patch("oz_workflows.artifacts.time.sleep", return_value=None)
    def test_times_out_when_artifact_missing(
        self, _mock_sleep: MagicMock, mock_build_client: MagicMock
    ) -> None:
        run = MagicMock()
        run.artifacts = []
        client = MagicMock()
        client.agent.runs.retrieve.return_value = run
        mock_build_client.return_value = client

        with self.assertRaises(RuntimeError) as ctx:
            poll_for_artifact(
                "run-123",
                filename="review.json",
                timeout_seconds=0,
                poll_interval_seconds=0,
            )
        self.assertIn("Timed out", str(ctx.exception))

    @patch("oz_workflows.artifacts.build_oz_client")
    @patch("oz_workflows.artifacts._download_artifact_text")
    def test_returns_text_artifact_when_present(
        self, mock_download: MagicMock, mock_build_client: MagicMock
    ) -> None:
        mock_download.return_value = "PR body"

        run = MagicMock()
        run.artifacts = [_make_artifact("FILE", "pr_description.md", "uid-pr")]
        client = MagicMock()
        client.agent.runs.retrieve.return_value = run
        mock_build_client.return_value = client

        result = poll_for_text_artifact(
            "run-123",
            filename="pr_description.md",
            timeout_seconds=0,
        )
        self.assertEqual(result, "PR body")


class LoadPrMetadataArtifactTest(unittest.TestCase):
    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_returns_valid_metadata(self, mock_poll: MagicMock) -> None:
        expected = {
            "branch_name": "oz-agent/implement-issue-42-add-retry",
            "pr_title": "fix: add retry logic",
            "pr_summary": "Closes #42\n\n## Summary\nAdded retry.",
        }
        mock_poll.return_value = expected
        result = load_pr_metadata_artifact("run-456")
        self.assertEqual(result, expected)
        # ``load_pr_metadata_artifact`` is a thin wrapper around
        # ``load_run_artifact`` (which delegates to ``poll_for_artifact``
        # with the default polling parameters).
        mock_poll.assert_called_once_with(
            "run-456",
            filename="pr-metadata.json",
            timeout_seconds=120,
            poll_interval_seconds=5,
        )

    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_raises_when_missing_keys(self, mock_poll: MagicMock) -> None:
        # Missing pr_summary
        mock_poll.return_value = {
            "branch_name": "oz-agent/implement-issue-42",
            "pr_title": "feat: something",
        }
        with self.assertRaises(RuntimeError) as ctx:
            load_pr_metadata_artifact("run-456")
        self.assertIn("pr_summary", str(ctx.exception))

    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_raises_when_all_keys_missing(self, mock_poll: MagicMock) -> None:
        mock_poll.return_value = {"extra": "value"}
        with self.assertRaises(RuntimeError) as ctx:
            load_pr_metadata_artifact("run-456")
        self.assertIn("branch_name", str(ctx.exception))
        self.assertIn("pr_title", str(ctx.exception))
        self.assertIn("pr_summary", str(ctx.exception))

    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_raises_when_pr_summary_empty(self, mock_poll: MagicMock) -> None:
        mock_poll.return_value = {
            "branch_name": "oz-agent/implement-issue-42",
            "pr_title": "feat: something",
            "pr_summary": "   ",
        }
        with self.assertRaises(RuntimeError) as ctx:
            load_pr_metadata_artifact("run-456")
        self.assertIn("empty pr_summary", str(ctx.exception))

    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_allows_extra_keys(self, mock_poll: MagicMock) -> None:
        metadata = {
            "branch_name": "oz-agent/implement-issue-42",
            "pr_title": "feat: new thing",
            "pr_summary": "Closes #42\n\nSummary.",
            "extra_field": "ignored",
        }
        mock_poll.return_value = metadata
        result = load_pr_metadata_artifact("run-456")
        self.assertEqual(result, metadata)


class WorkflowFilenameConstantsTest(unittest.TestCase):
    """Lock in the workflow-specific artifact filenames.

    The cloud-mode prompts and the cron poller both refer to these
    filenames, so a typo or rename has to be intentional.
    """

    def test_filenames_are_canonical(self) -> None:
        self.assertEqual(PR_METADATA_FILENAME, "pr-metadata.json")
        self.assertEqual(TRIAGE_RESULT_FILENAME, "triage_result.json")
        self.assertEqual(ISSUE_RESPONSE_FILENAME, "issue_response.json")
        self.assertEqual(REVIEW_FILENAME, "review.json")


class LoadRunArtifactTest(unittest.TestCase):
    """Coverage for the workflow-agnostic :func:`load_run_artifact`."""

    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_forwards_filename_and_polling_kwargs(
        self, mock_poll: MagicMock
    ) -> None:
        mock_poll.return_value = {"k": "v"}
        result = load_run_artifact(
            "run-1",
            filename="custom.json",
            timeout_seconds=200,
            poll_interval_seconds=7,
        )
        self.assertEqual(result, {"k": "v"})
        mock_poll.assert_called_once_with(
            "run-1",
            filename="custom.json",
            timeout_seconds=200,
            poll_interval_seconds=7,
        )

    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_uses_default_polling_kwargs(self, mock_poll: MagicMock) -> None:
        mock_poll.return_value = {}
        load_run_artifact("run-1", filename="custom.json")
        mock_poll.assert_called_once_with(
            "run-1",
            filename="custom.json",
            timeout_seconds=120,
            poll_interval_seconds=5,
        )

    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_propagates_polling_failure(self, mock_poll: MagicMock) -> None:
        # When the underlying poller raises (timeout, transport error,
        # malformed JSON), ``load_run_artifact`` lets the exception
        # surface so the caller can handle it explicitly.
        mock_poll.side_effect = RuntimeError("timed out")
        with self.assertRaises(RuntimeError):
            load_run_artifact("run-1", filename="custom.json")


class NamedArtifactWrapperTest(unittest.TestCase):
    """The named wrappers must dispatch to the right filename.

    The triage / respond-to-triaged / review pipelines all live in
    different scripts; locking the filename here makes sure the agent
    upload command and the cron poller stay in sync.
    """

    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_load_triage_artifact_uses_triage_result_json(
        self, mock_poll: MagicMock
    ) -> None:
        mock_poll.return_value = {"summary": "triaged", "labels": ["bug"]}
        result = load_triage_artifact("run-1")
        self.assertEqual(result, {"summary": "triaged", "labels": ["bug"]})
        mock_poll.assert_called_once_with(
            "run-1",
            filename="triage_result.json",
            timeout_seconds=120,
            poll_interval_seconds=5,
        )

    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_load_issue_response_artifact_uses_issue_response_json(
        self, mock_poll: MagicMock
    ) -> None:
        mock_poll.return_value = {"analysis_comment": "hi"}
        result = load_issue_response_artifact("run-1")
        self.assertEqual(result, {"analysis_comment": "hi"})
        mock_poll.assert_called_once_with(
            "run-1",
            filename="issue_response.json",
            timeout_seconds=120,
            poll_interval_seconds=5,
        )

    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_load_review_artifact_uses_review_json(
        self, mock_poll: MagicMock
    ) -> None:
        mock_poll.return_value = {"summary": "LGTM", "comments": []}
        result = load_review_artifact("run-1")
        self.assertEqual(result, {"summary": "LGTM", "comments": []})
        mock_poll.assert_called_once_with(
            "run-1",
            filename="review.json",
            timeout_seconds=120,
            poll_interval_seconds=5,
        )


class TryLoadPrMetadataArtifactTest(unittest.TestCase):
    """Tests covering exception-handling for ``try_load_pr_metadata_artifact``.

    Validation of well-formed payloads (missing keys, empty strings, and
    accepted metadata shapes) is covered by ``LoadPrMetadataArtifactTest``
    against the underlying ``load_pr_metadata_artifact``; these tests focus
    on the ``try_*`` wrapper's contract of returning ``None`` instead of
    raising when the underlying load fails.
    """

    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_returns_metadata_when_artifact_valid(self, mock_poll: MagicMock) -> None:
        expected = {
            "branch_name": "oz-agent/spec-issue-42",
            "pr_title": "feat: implement spec",
            "pr_summary": "Closes #42\n\n## Summary\nImplemented the spec.",
        }
        mock_poll.return_value = expected
        result = try_load_pr_metadata_artifact(
            "run-abc", timeout_seconds=0, poll_interval_seconds=0
        )
        self.assertEqual(result, expected)
        mock_poll.assert_called_once_with(
            "run-abc",
            filename="pr-metadata.json",
            timeout_seconds=0,
            poll_interval_seconds=0,
        )

    @patch("oz_workflows.artifacts.poll_for_artifact")
    def test_returns_none_on_recoverable_load_failures(
        self, mock_poll: MagicMock
    ) -> None:
        """All recoverable load failures return ``None`` instead of raising."""
        request = httpx.Request("GET", "https://example.test/signed")
        response = MagicMock(spec=httpx.Response, status_code=404, request=request)
        cases = [
            (
                "artifact_missing_timeout",
                RuntimeError("Timed out waiting for FILE artifact"),
            ),
            (
                "malformed_json",
                json.JSONDecodeError("malformed", "doc", 0),
            ),
            (
                "http_status_error",
                httpx.HTTPStatusError(
                    "not found", request=request, response=response
                ),
            ),
        ]
        for label, exc in cases:
            with self.subTest(label=label):
                mock_poll.reset_mock()
                mock_poll.side_effect = exc
                result = try_load_pr_metadata_artifact(
                    "run-abc", timeout_seconds=0, poll_interval_seconds=0
                )
                self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
