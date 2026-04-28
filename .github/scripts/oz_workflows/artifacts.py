from __future__ import annotations

import json
import random
import time
from typing import Any, Protocol, TypedDict, cast

import httpx
from oz_agent_sdk import OzAPI
from oz_agent_sdk.types import AgentGetArtifactResponse
from oz_agent_sdk.types.agent import RunItem

from .oz_client import build_oz_client

# Retry policy for artifact downloads. A transient CDN or S3 blip can surface as
# either a 5xx response or as a network-level exception (connection reset, DNS
# flake, read timeout, etc.). We want to retry a handful of times with
# exponential backoff + jitter so a momentary failure at the tail end of an
# otherwise successful agent run does not cause the entire workflow to fail.
_DOWNLOAD_MAX_ATTEMPTS = 5
_DOWNLOAD_INITIAL_BACKOFF_SECONDS = 1.0
_DOWNLOAD_MAX_BACKOFF_SECONDS = 10.0

# Network-level httpx exceptions that are worth retrying. These cover the
# common transient failures for signed-URL downloads.
_RETRYABLE_NETWORK_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


class PrMetadata(TypedDict):
    """Structured PR metadata produced by implementation workflows."""

    branch_name: str
    pr_title: str
    pr_summary: str


class ResolvedReviewComment(TypedDict):
    """A single PR review comment that the agent reported as resolved."""

    comment_id: int
    summary: str


class _FileArtifactDataLike(Protocol):
    artifact_uid: str
    filename: str | None


class _FileArtifactLike(Protocol):
    artifact_type: str
    data: _FileArtifactDataLike | None


def poll_for_artifact(
    run_id: str,
    *,
    filename: str,
    timeout_seconds: int = 120,
    poll_interval_seconds: int = 5,
) -> dict[str, Any]:
    """Retrieve a FILE artifact by filename from a completed Oz run.

    The caller should invoke this after ``run_agent()`` has returned
    (i.e. the run has reached a terminal SUCCEEDED state).  The artifact
    should already be present, but we poll briefly for resilience against
    propagation delay.
    """
    client = build_oz_client()
    artifact_uid = _poll_for_file_artifact_uid(
        client,
        run_id,
        filename=filename,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    return _download_artifact_json(client, artifact_uid)


def poll_for_text_artifact(
    run_id: str,
    *,
    filename: str,
    timeout_seconds: int = 120,
    poll_interval_seconds: int = 5,
) -> str:
    """Retrieve a FILE artifact by filename and return its raw text content."""
    client = build_oz_client()
    artifact_uid = _poll_for_file_artifact_uid(
        client,
        run_id,
        filename=filename,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    return _download_artifact_text(client, artifact_uid)


def _poll_for_file_artifact_uid(
    client: OzAPI,
    run_id: str,
    *,
    filename: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> str:
    """Wait for a FILE artifact by filename and return its artifact UID."""
    deadline = time.monotonic() + timeout_seconds

    while True:
        run = client.agent.runs.retrieve(run_id)
        artifact_uid = _find_file_artifact(run, filename)
        if artifact_uid is not None:
            return artifact_uid
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Timed out waiting for FILE artifact '{filename}' on Oz run {run_id}"
            )
        time.sleep(poll_interval_seconds)


def _find_file_artifact(run: RunItem, filename: str) -> str | None:
    """Return the artifact UID for a FILE artifact matching *filename*, or None."""
    artifacts = cast(list[_FileArtifactLike], run.artifacts or [])
    for artifact in artifacts:
        if artifact.artifact_type != "FILE":
            continue
        data = artifact.data
        if data is None:
            continue
        if data.filename == filename:
            return str(data.artifact_uid)
    return None


def _download_artifact_json(client: OzAPI, artifact_uid: str) -> dict[str, Any]:
    """Fetch a FILE artifact's signed URL and download its JSON content."""
    payload = json.loads(_download_artifact_text(client, artifact_uid))
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Artifact {artifact_uid} must decode to a JSON object"
        )
    return payload


def _download_artifact_text(client: OzAPI, artifact_uid: str) -> str:
    """Fetch a FILE artifact's signed URL and download its text content.

    The download is retried with exponential backoff + jitter on 5xx
    responses and on transient httpx network errors (connect/read timeouts,
    protocol errors, etc.). 4xx responses are not retried and surface
    immediately as ``httpx.HTTPStatusError``.
    """
    response: AgentGetArtifactResponse = client.agent.get_artifact(artifact_uid)
    download_url = response.data.download_url
    if not download_url:
        raise RuntimeError(
            f"Artifact {artifact_uid} did not return a download URL"
        )
    with httpx.Client(timeout=30) as http:
        return _download_text_with_retries(http, download_url, artifact_uid)


def _download_text_with_retries(
    http: httpx.Client, download_url: str, artifact_uid: str
) -> str:
    """GET *download_url* with retries on 5xx and transient network errors.

    Returns the response text on success. Raises the last encountered error
    after ``_DOWNLOAD_MAX_ATTEMPTS`` failed attempts.
    """
    last_error: Exception | None = None
    for attempt in range(_DOWNLOAD_MAX_ATTEMPTS):
        try:
            download_response = http.get(download_url)
        except _RETRYABLE_NETWORK_EXCEPTIONS as exc:
            last_error = exc
        else:
            if download_response.status_code < 500:
                # 2xx returns the body; 4xx raises a non-retryable error.
                download_response.raise_for_status()
                return download_response.text
            last_error = httpx.HTTPStatusError(
                (
                    f"Server error {download_response.status_code} while "
                    f"downloading artifact {artifact_uid}"
                ),
                request=download_response.request,
                response=download_response,
            )

        if attempt >= _DOWNLOAD_MAX_ATTEMPTS - 1:
            break
        backoff = min(
            _DOWNLOAD_INITIAL_BACKOFF_SECONDS * (2**attempt),
            _DOWNLOAD_MAX_BACKOFF_SECONDS,
        )
        # Add jitter to avoid thundering-herd style retry storms across
        # concurrently-running workflows.
        time.sleep(backoff + random.uniform(0, 1))

    # At least one attempt always runs, so last_error is set when we exit
    # the loop without returning. Guard against the theoretical case where
    # it isn't so we don't raise ``TypeError`` under ``python -O`` (which
    # strips ``assert`` statements).
    if last_error is None:
        raise RuntimeError(
            f"Exhausted retries downloading artifact {artifact_uid} "
            "without recording an error"
        )
    raise last_error


PR_METADATA_FILENAME = "pr-metadata.json"
TRIAGE_RESULT_FILENAME = "triage_result.json"
ISSUE_RESPONSE_FILENAME = "issue_response.json"
REVIEW_FILENAME = "review.json"

_PR_METADATA_REQUIRED_KEYS = ("branch_name", "pr_title", "pr_summary")


def load_run_artifact(
    run_id: str,
    *,
    filename: str,
    timeout_seconds: int = 120,
    poll_interval_seconds: int = 5,
) -> dict[str, Any]:
    """Load a named JSON artifact from a completed Oz run.

    This is the workflow-agnostic entry point named in the cloud-mode
    plan: callers identify the artifact by the filename the agent
    uploaded via ``oz artifact upload <name>.json`` and the helper
    polls the run's artifact list until the matching FILE artifact
    appears, then downloads its signed URL and JSON-decodes the body.

    Workflow-specific wrappers below (:func:`load_triage_artifact`,
    :func:`load_issue_response_artifact`, :func:`load_review_artifact`,
    :func:`load_pr_metadata_artifact`) layer on top of this function so
    the per-workflow result schemas validate consistently while sharing
    the same artifact-fetch pipeline.
    """
    return poll_for_artifact(
        run_id,
        filename=filename,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


def load_triage_artifact(run_id: str) -> dict[str, Any]:
    """Load the ``triage_result.json`` artifact for a completed triage run.

    Schema validation lives in ``triage_new_issues.apply_triage_result``
    and the various ``extract_*`` helpers, which already tolerate
    missing/extra keys; this loader keeps the contract narrow and
    returns the raw decoded JSON object.
    """
    return load_run_artifact(run_id, filename=TRIAGE_RESULT_FILENAME)


def load_issue_response_artifact(run_id: str) -> dict[str, Any]:
    """Load the ``issue_response.json`` artifact for a respond-to-triaged run."""
    return load_run_artifact(run_id, filename=ISSUE_RESPONSE_FILENAME)


def load_review_artifact(run_id: str) -> dict[str, Any]:
    """Load the ``review.json`` artifact for a completed PR review run.

    The PR review pipeline normalizes the payload via
    ``review_pr._normalize_review_payload`` after this loader returns,
    which is where the strict ``summary``/``comments`` schema check
    lives. This loader stays a thin wrapper so the cron poller can call
    it without coupling to the review-specific normalization.
    """
    return load_run_artifact(run_id, filename=REVIEW_FILENAME)


def load_pr_metadata_artifact(run_id: str) -> PrMetadata:
    """Load and validate the pr-metadata.json artifact from a completed Oz run.

    Implemented as a thin wrapper around :func:`load_run_artifact` that
    additionally enforces the ``{branch_name, pr_title, pr_summary}``
    keys spec and trims-non-empty-string check on ``pr_summary`` so
    spec/implementation workflows can rely on a structured
    :class:`PrMetadata` value.
    """
    metadata = load_run_artifact(run_id, filename=PR_METADATA_FILENAME)
    missing = [key for key in _PR_METADATA_REQUIRED_KEYS if key not in metadata]
    if missing:
        raise RuntimeError(
            f"pr-metadata.json artifact from Oz run {run_id} is missing "
            f"required key(s): {', '.join(missing)}"
        )
    pr_summary = metadata.get("pr_summary", "")
    if not isinstance(pr_summary, str) or not pr_summary.strip():
        raise RuntimeError(
            f"pr-metadata.json artifact from Oz run {run_id} has an empty pr_summary"
        )
    return cast(PrMetadata, metadata)


def try_load_pr_metadata_artifact(
    run_id: str,
    *,
    timeout_seconds: int = 10,
    poll_interval_seconds: int = 2,
) -> PrMetadata | None:
    """Try to load the optional ``pr-metadata.json`` artifact.

    Workflows that only *sometimes* need to refresh the PR title/body (for
    example, ``respond-to-pr-comment`` when the agent's changes transition
    a spec-only PR into a spec + implementation PR) should use this helper
    rather than ``load_pr_metadata_artifact`` so a missing or malformed
    artifact degrades to ``None`` instead of aborting the workflow.

    Uses a short polling window by default because the artifact is
    optional. When the artifact is absent, the agent did not intend to
    refresh the PR description and callers should leave the existing
    description untouched.
    """
    try:
        metadata = poll_for_artifact(
            run_id,
            filename=PR_METADATA_FILENAME,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
    except (RuntimeError, ValueError, httpx.HTTPError):
        # ``RuntimeError``: poll timeouts or non-object JSON payloads.
        # ``ValueError``: malformed JSON (``json.JSONDecodeError`` is a subclass).
        # ``httpx.HTTPError``: 4xx responses or other transport-level failures
        # that survived the download retries in ``_download_artifact_text``.
        return None
    missing = [key for key in _PR_METADATA_REQUIRED_KEYS if key not in metadata]
    if missing:
        return None
    pr_summary = metadata.get("pr_summary", "")
    if not isinstance(pr_summary, str) or not pr_summary.strip():
        return None
    pr_title = metadata.get("pr_title", "")
    if not isinstance(pr_title, str) or not pr_title.strip():
        return None
    return cast(PrMetadata, metadata)


RESOLVED_REVIEW_COMMENTS_FILENAME = "resolved_review_comments.json"


def _normalize_resolved_review_comment_entry(
    entry: Any, *, index: int
) -> ResolvedReviewComment | None:
    """Normalize a raw ``resolved_review_comments`` entry from the artifact.

    Returns ``None`` (logging a warning) when the entry cannot be coerced into
    the documented ``{"comment_id": int, "summary": str}`` shape rather than
    raising, so a single malformed entry does not abort the workflow.
    """
    if not isinstance(entry, dict):
        print(
            f"[resolved-review-comments] Dropped entry {index}: expected object, got {type(entry).__name__}"
        )
        return None
    raw_comment_id = entry.get("comment_id")
    comment_id: int | None
    if isinstance(raw_comment_id, bool):
        # ``bool`` is a subclass of ``int``; treat it as invalid.
        comment_id = None
    elif isinstance(raw_comment_id, int):
        comment_id = raw_comment_id
    elif isinstance(raw_comment_id, str) and raw_comment_id.strip().isdigit():
        comment_id = int(raw_comment_id.strip())
    else:
        comment_id = None
    if comment_id is None or comment_id <= 0:
        print(
            f"[resolved-review-comments] Dropped entry {index}: missing or invalid `comment_id`"
        )
        return None
    summary = entry.get("summary")
    if not isinstance(summary, str):
        summary = ""
    summary = summary.strip()
    if not summary:
        print(
            f"[resolved-review-comments] Dropped entry {index} for comment {comment_id}: missing `summary`"
        )
        return None
    return {"comment_id": comment_id, "summary": summary}


def normalize_resolved_review_comments_payload(
    payload: Any,
) -> list[ResolvedReviewComment]:
    """Validate and normalize a ``resolved_review_comments.json`` payload.

    Accepts either an object with a ``resolved_review_comments`` list or a
    bare list of entries. Dropped entries (malformed or duplicate
    ``comment_id``) are logged and skipped so the rest of the workflow
    continues uninterrupted.
    """
    if isinstance(payload, dict):
        raw_entries = payload.get("resolved_review_comments")
    else:
        raw_entries = payload
    if raw_entries is None:
        return []
    if not isinstance(raw_entries, list):
        print(
            "[resolved-review-comments] Dropping payload: `resolved_review_comments` must be a list"
        )
        return []
    seen: set[int] = set()
    resolved: list[ResolvedReviewComment] = []
    for index, entry in enumerate(raw_entries):
        normalized = _normalize_resolved_review_comment_entry(entry, index=index)
        if normalized is None:
            continue
        if normalized["comment_id"] in seen:
            print(
                f"[resolved-review-comments] Dropped duplicate entry for comment {normalized['comment_id']}"
            )
            continue
        seen.add(normalized["comment_id"])
        resolved.append(normalized)
    return resolved


def try_load_resolved_review_comments_artifact(
    run_id: str,
    *,
    timeout_seconds: int = 10,
    poll_interval_seconds: int = 2,
) -> list[ResolvedReviewComment]:
    """Try to load the optional ``resolved_review_comments.json`` artifact.

    The artifact is emitted by the agent only when it resolved one or more
    PR review comments as part of the run. When it is absent (or cannot be
    parsed), this returns an empty list rather than raising so callers can
    fall back to their existing completion behavior.
    """
    try:
        payload = poll_for_artifact(
            run_id,
            filename=RESOLVED_REVIEW_COMMENTS_FILENAME,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
    except (RuntimeError, ValueError, httpx.HTTPError):
        # ``RuntimeError``: poll timeouts or non-object JSON payloads.
        # ``ValueError``: malformed JSON (``json.JSONDecodeError`` is a subclass).
        # ``httpx.HTTPError``: 4xx responses or other transport-level failures
        # that survived the download retries in ``_download_artifact_text``.
        # Any of these should degrade to an empty list so a broken optional
        # artifact never aborts the surrounding workflow's success path.
        return []
    return normalize_resolved_review_comments_payload(payload)
