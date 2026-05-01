"""Run an Oz agent inside a Docker container from the GitHub Actions runner.

This is the Docker-based counterpart to :func:`oz_workflows.oz_client.run_agent`.
Instead of dispatching the agent to a pre-defined Warp cloud environment,
the caller invokes a locally-built image (e.g. ``oz-for-oss-triage``) that
bundles the ``oz`` CLI. The container reads the consuming repo via a
read-only mount at ``/mnt/repo`` and writes its structured result into a
writable mount at ``/mnt/output``.

The helper streams stdout line-by-line, parses each JSON event emitted by
``oz agent run --output-format json``, and surfaces the run id and
session-share link to an optional ``on_event`` callback so existing
progress-comment plumbing keeps working.

Event schema
------------
The serialized shape of every JSON line the CLI emits lives in the Rust
``JsonMessage`` / ``JsonSystemEvent`` enums in
``warp-internal/deep-forest/app/src/ai/agent_sdk/driver/output.rs``
(see ``pub mod json`` around line 532). Those enums are deliberately kept
as a stable, serde-tagged interface for external consumers and are not
1:1 with the internal ``AIAgent*`` types.

The three ``type="system"`` events this module consumes, with their
emit sites and the condition that causes each to fire, are:

* ``event_type="run_started"``, payload ``{run_id, run_url}`` -- emitted
  unconditionally on every ``oz agent run`` invocation by
  ``AgentDriverRunner::setup_and_run_driver`` via ``driver::write_run_started``
  (``agent_sdk/mod.rs``, calls ``output::json::run_started`` in
  ``driver.rs``'s ``write_run_started``). The CLI always assigns a task
  id before the driver starts, so this event is guaranteed for every run.
* ``event_type="shared_session_established"``, payload ``{join_url}`` --
  emitted when the terminal driver reports a successful share handshake
  (``AgentDriver::handle_terminal_driver_event`` ->
  ``write_session_joined`` in ``driver.rs``). It is only emitted when
  ``--share`` is passed; ``_build_docker_argv`` always adds
  ``--share public:view`` so we rely on this event to populate
  ``session_link``.
* ``event_type="conversation_started"``, payload ``{conversation_id}`` --
  emitted once per run when the first server conversation token arrives,
  from the ``BlocklistAIHistoryEvent::UpdatedStreamingExchange`` handler
  in ``driver.rs``. Expected exactly once for any run that reaches the
  server, so a missing value signals the run failed before any model
  round-trip.

Other events the CLI may emit on stdout (``type="agent"``,
``type="agent_reasoning"``, ``type="tool_call"``, ``type="tool_result"``,
``type="skill_invoked"``, ``type="artifact_created"``, etc.) are
forwarded to ``on_event`` without being inspected, so callers can extend
parsing without modifying this module. The parser is tolerant: any event
whose ``event_type`` we do not recognize is a no-op, so additions to the
Rust enum do not break existing runs.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .actions import notice
from .env import optional_env

logger = logging.getLogger(__name__)

# Default timeout for an agent run. The triage workflow's SDK path uses
# ``60 * 60`` seconds; keep parity so we don't tighten the limit by
# accident when moving into Docker.
DEFAULT_TIMEOUT_SECONDS = 60 * 60

# Mount paths inside the container. The Dockerfile's documentation and
# the ``triage-issue`` skill's Docker workflow mode reference these same
# constants; changing them requires updating the skill as well.
REPO_MOUNT = "/mnt/repo"
OUTPUT_MOUNT = "/mnt/output"


@dataclass
class DockerAgentRun:
    """Structured result for a completed :func:`run_agent_in_docker` invocation.

    ``run_id`` and ``session_link`` mirror the ``RunItem`` fields consumed
    by :func:`oz_workflows.helpers.record_run_session_link` so callers can
    reuse the same progress-comment plumbing. ``output`` holds the parsed
    JSON the agent wrote to ``/mnt/output/<output_filename>``; the helper
    reads and cleans up the backing tempdir before returning, so callers
    never need to touch a filesystem path.
    """

    run_id: str = ""
    session_link: str = ""
    conversation_id: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0


class DockerAgentError(RuntimeError):
    """Raised when the Docker-based agent run fails before reporting a result."""


class DockerAgentTimeout(DockerAgentError):
    """Raised when the agent container exceeds the configured timeout."""


def run_agent_in_docker(
    *,
    prompt: str,
    skill_name: str,
    title: str,
    image: str,
    repo_dir: Path | str,
    output_filename: str,
    on_event: Callable[[DockerAgentRun], None] | None = None,
    model: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    log_group: str | None = None,
    repo_read_only: bool = True,
    forward_env_names: Iterable[str] | None = None,
) -> DockerAgentRun:
    """Run ``oz agent run`` inside *image* and return the final run state.

    The helper:

    1. Creates a temporary output directory on the host and mounts it at
       ``/mnt/output`` in the container. The agent is instructed (via the
       prompt / skill) to write *output_filename* into that directory.
    2. Spawns ``docker run --rm`` with a read-only repo mount, streams
       stdout to the host's stdout, and parses JSON-line events to track
       the run id and session-share link.
    3. Enforces *timeout_seconds* using a ``threading.Timer`` so we don't
       hang the workflow if the container stops responding.
    4. Reads and JSON-decodes *output_filename* from the output dir,
       stashes the parsed payload on ``run.output``, and deletes the
       tempdir before returning. Callers read ``run.output`` directly;
       no filesystem state survives this call.

    Raises :class:`DockerAgentError` on a non-zero exit code, a missing
    or malformed output file, or any other failure before ``run.output``
    is populated. Raises :class:`DockerAgentTimeout` when the watchdog
    fires. Either way, the output directory is removed.
    """
    repo_path = Path(repo_dir).resolve()
    if not repo_path.is_dir():
        raise DockerAgentError(
            f"Docker agent repo directory does not exist: {repo_path}"
        )

    output_dir = Path(tempfile.mkdtemp(prefix="oz-agent-output-"))

    # We only log the group banner when the caller asked for one. The
    # GitHub Actions ``::group::`` annotation is idempotent - using it
    # from local tools (e.g. ``scripts/local_triage.py``) is harmless.
    group_label = (log_group or title).strip()
    if group_label:
        print(f"::group::{group_label}", flush=True)

    run = DockerAgentRun()
    try:
        argv = _build_docker_argv(
            image=image,
            repo_dir=repo_path,
            output_dir=output_dir,
            prompt=prompt,
            skill_name=skill_name,
            title=title,
            model=model,
            repo_read_only=repo_read_only,
            forward_env_names=forward_env_names,
        )
        notice(f"Launching agent container: {_format_argv_for_log(argv)}")
        _run_and_stream(
            argv,
            run=run,
            on_event=on_event,
            timeout_seconds=timeout_seconds,
        )

        if run.exit_code != 0:
            raise DockerAgentError(
                f"Docker agent exited with code {run.exit_code} (image={image})"
            )
        run.output = _read_output_json(output_dir / output_filename)
        return run
    finally:
        if group_label:
            print("::endgroup::", flush=True)
        # Unconditional cleanup so callers (including
        # ``scripts/local_triage.py`` and any future local tooling) never
        # have to track or remove the backing tempdir themselves.
        shutil.rmtree(output_dir, ignore_errors=True)


def _build_docker_argv(
    *,
    image: str,
    repo_dir: Path,
    output_dir: Path,
    prompt: str,
    skill_name: str,
    title: str,
    model: str | None,
    repo_read_only: bool,
    forward_env_names: Iterable[str] | None,
) -> list[str]:
    """Build the ``docker run`` argv for the triage container.

    Environment variables that the container needs are forwarded via
    ``-e <NAME>`` (the host's value is inherited). We intentionally never
    forward the value inline so ``WARP_API_KEY`` never appears in process
    listings.
    """
    argv: list[str] = ["docker", "run", "--rm"]
    env_names = tuple(forward_env_names or ("WARP_API_KEY", "WARP_API_BASE_URL"))
    for name in env_names:
        argv.extend(["-e", name])
    repo_mount = (
        f"{repo_dir}:{REPO_MOUNT}:ro"
        if repo_read_only
        else f"{repo_dir}:{REPO_MOUNT}"
    )

    argv.extend(
        [
            "-v",
            repo_mount,
            "-v",
            f"{output_dir}:{OUTPUT_MOUNT}",
            image,
            "agent",
            "run",
            "--skill",
            skill_name,
            "--cwd",
            REPO_MOUNT,
            "--prompt",
            prompt,
            "--output-format",
            "json",
            "--name",
            title,
            "--share",
            "public:view",
        ]
    )
    if model:
        argv.extend(["--model", model])
    return argv


def _run_and_stream(
    argv: list[str],
    *,
    run: DockerAgentRun,
    on_event: Callable[[DockerAgentRun], None] | None,
    timeout_seconds: int,
) -> None:
    """Spawn the container, stream stdout, and parse events.

    stderr is merged into stdout via ``stderr=subprocess.STDOUT``. The
    previous ``stderr=subprocess.PIPE`` shape could deadlock if the
    container wrote more than the pipe buffer (~64KB on Linux) before
    exiting, since this loop only drained stdout. Merging keeps the
    drain single-threaded and gives operators one contiguous log stream
    to read.
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    timed_out = False

    def _kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        proc.kill()

    timer = threading.Timer(timeout_seconds, _kill_on_timeout)
    timer.start()
    try:
        assert proc.stdout is not None  # for the type checker
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            _ingest_stdout_line(line, run=run, on_event=on_event)
        proc.wait()
    finally:
        timer.cancel()

    if timed_out:
        raise DockerAgentTimeout(
            f"Docker agent timed out after {timeout_seconds} seconds"
        )

    run.exit_code = int(proc.returncode or 0)


def _ingest_stdout_line(
    line: str,
    *,
    run: DockerAgentRun,
    on_event: Callable[[DockerAgentRun], None] | None,
) -> None:
    """Parse a single stdout line and update *run* when it's a known event.

    Only ``type="system"`` events that actually change a tracked field
    (``run_id``, ``session_link``, ``conversation_id``) trigger the
    ``on_event`` callback. Noisier events -- ``agent``, ``tool_call``,
    ``tool_result``, ``skill_invoked``, etc. -- are ignored here because
    callbacks like ``_record_triage_session_link`` issue a GitHub
    ``PATCH /comments/:id`` per invocation and would otherwise hit
    comment-edit rate limits on chatty runs. The raw stdout is already
    echoed back to the host's stdout so operators can still see
    everything during the run.
    """
    stripped = line.strip()
    if not stripped:
        return
    try:
        event = json.loads(stripped)
    except ValueError:
        return
    if not isinstance(event, dict):
        return

    if event.get("type") != "system":
        return
    if not _apply_system_event(event, run=run):
        return
    if on_event is None:
        return
    try:
        on_event(run)
    except Exception:
        logger.exception("Docker agent on_event callback raised")


def _apply_system_event(event: dict[str, Any], *, run: DockerAgentRun) -> bool:
    """Apply a ``{"type": "system", "event_type": ...}`` payload to *run*.

    Returns ``True`` iff the event actually changed a tracked field on
    *run* (so callers can fire ``on_event`` only on real state
    transitions). The three recognized ``event_type`` values come from
    the ``JsonSystemEvent`` Rust enum in
    ``deep-forest/app/src/ai/agent_sdk/driver/output.rs``:
    ``run_started``, ``shared_session_established``, and
    ``conversation_started``. See the module docstring for emit sites
    and guarantees. Any other value is silently ignored (returns
    ``False``) so new variants added upstream do not break the parser.
    """
    event_type = event.get("event_type")
    if event_type == "run_started":
        run_id = str(event.get("run_id") or "").strip()
        if run_id and run.run_id != run_id:
            run.run_id = run_id
            return True
    elif event_type == "shared_session_established":
        join_url = str(event.get("join_url") or "").strip()
        if join_url and run.session_link != join_url:
            run.session_link = join_url
            return True
    elif event_type == "conversation_started":
        conversation_id = str(event.get("conversation_id") or "").strip()
        if conversation_id and run.conversation_id != conversation_id:
            run.conversation_id = conversation_id
            return True
    return False


def _format_argv_for_log(argv: Iterable[str]) -> str:
    """Produce a single-line representation of *argv* safe for logs.

    The prompt is potentially large and noisy, so we replace it with a
    short ``<prompt bytes>`` placeholder. Every other argument is emitted
    verbatim; forwarded env vars use the bare ``-e NAME`` form so the
    secret value never lives on the argv in the first place.
    """
    rendered: list[str] = []
    skip_next = False
    for part in argv:
        if skip_next:
            rendered.append("<prompt bytes>")
            skip_next = False
            continue
        if part == "--prompt":
            rendered.append(part)
            skip_next = True
            continue
        rendered.append(part)
    return " ".join(rendered)


def _read_output_json(path: Path) -> dict[str, Any]:
    """Read and JSON-decode *path*, raising ``DockerAgentError`` on problems.

    Internal helper used by :func:`run_agent_in_docker` to pull the
    agent's result JSON out of the mounted output directory before the
    tempdir is removed. Returns a JSON object (``dict``); raises when
    the file is missing, unreadable, malformed, or not a JSON object.
    """
    if not path.is_file():
        raise DockerAgentError(
            f"Docker agent did not produce expected output file: {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise DockerAgentError(
            f"Docker agent output file {path} did not decode as JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise DockerAgentError(
            f"Docker agent output file {path} must decode to a JSON object"
        )
    return data


def resolve_triage_image() -> str:
    """Return the image tag the triage workflows use.

    Workflows set ``TRIAGE_IMAGE`` in the job env. The fallback matches
    the tag produced by the ``docker build`` step.
    """
    return optional_env("TRIAGE_IMAGE") or "oz-for-oss-triage"


def resolve_review_image() -> str:
    """Return the image tag the PR review workflow uses."""
    return optional_env("REVIEW_IMAGE") or "oz-for-oss-review"


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DockerAgentError",
    "DockerAgentRun",
    "DockerAgentTimeout",
    "OUTPUT_MOUNT",
    "REPO_MOUNT",
    "resolve_review_image",
    "resolve_triage_image",
    "run_agent_in_docker",
]
