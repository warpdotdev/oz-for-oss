"""Vercel cron entrypoint.

Vercel cron triggers hit ``/api/cron`` on the schedule defined in
``vercel.json``. The handler reads in-flight run state from KV, polls
the Oz API for terminal status, and applies the result back to GitHub
via the registered :class:`~control_plane.lib.poll_runs.WorkflowHandlers`.

The handler currently registers an empty handler map; concrete result
appliers (post-review, apply-triage-labels, post-issue-response, etc.)
will land alongside this scaffold during the cutover. Until then the
cron task drains malformed records and reports outcome counts, which
is enough to verify the plumbing end-to-end during deployment.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler
from typing import Any, Mapping

from lib.poll_runs import DrainOutcome, WorkflowHandlers, drain_in_flight_runs
from lib.state import StateStore

logger = logging.getLogger(__name__)


def _resolve_cron_secret() -> str | None:
    """Return the configured cron-secret, when set.

    Vercel cron requests include the ``Authorization: Bearer <secret>``
    header that matches the project's ``CRON_SECRET`` env var. The
    handler treats the secret as optional during local ``vercel dev``
    so test traffic does not require the production secret.
    """
    secret = os.environ.get("CRON_SECRET", "").strip()
    return secret or None


def build_state_store() -> StateStore:
    """Construct the production :class:`StateStore`.

    The Vercel KV adapter is wired in here; pulling the upstream KV SDK
    in keeps the import boundary at the entrypoint so unit tests don't
    need it on PYTHONPATH. Production deployments install
    ``upstash-vercel-python`` (or the in-house adapter) via
    :file:`requirements.txt`.
    """
    try:
        # ``vercel_kv`` is the binding name used by the official
        # Vercel KV Python SDK. Imported lazily because the test suite
        # runs without the package on PYTHONPATH.
        from vercel_kv import KV  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - production-only path
        raise RuntimeError(
            "vercel_kv is not installed; the production cron entrypoint "
            "needs the Vercel KV SDK to read in-flight run state."
        ) from exc

    kv = KV()

    class VercelKVStore:
        def put(self, key: str, value: str) -> None:
            kv.set(key, value)

        def get(self, key: str) -> str | None:
            value = kv.get(key)
            if value is None:
                return None
            return value if isinstance(value, str) else json.dumps(value)

        def delete(self, key: str) -> None:
            kv.delete(key)

        def keys(self, prefix: str) -> list[str]:
            return list(kv.scan(prefix))

    return VercelKVStore()


def build_workflow_handlers() -> Mapping[str, WorkflowHandlers]:
    """Return the workflow-handler registry used by the cron poller.

    The registry is empty in this scaffold: concrete handlers are
    introduced workflow-by-workflow as the control plane assumes
    responsibility for each one. Until then the poller drains records
    that no longer have a registered handler so KV does not grow
    unboundedly during the cutover.
    """
    return {}


def run_cron_tick(
    *,
    store: StateStore,
    retriever: Any,
    handlers: Mapping[str, WorkflowHandlers] | None = None,
) -> list[DrainOutcome]:
    """Process a single cron tick.

    Wired as a free function so unit tests can exercise the loop with a
    fake store and retriever. The Vercel ``handler`` calls this with
    production wiring.
    """
    return drain_in_flight_runs(
        store=store,
        retriever=retriever,
        handlers=handlers or build_workflow_handlers(),
    )


def _summarize(outcomes: list[DrainOutcome]) -> dict[str, Any]:
    counters: dict[str, int] = {}
    for outcome in outcomes:
        counters[outcome.state] = counters.get(outcome.state, 0) + 1
    return {
        "drained": len(outcomes),
        "applied": sum(1 for o in outcomes if o.applied),
        "states": counters,
        "outcomes": [asdict(o) for o in outcomes],
    }


class handler(BaseHTTPRequestHandler):  # noqa: N801 - Vercel requires this exact symbol name.
    server_version = "OzForOSSCron/1.0"

    def do_GET(self) -> None:  # noqa: N802 - signature comes from BaseHTTPRequestHandler.
        secret = _resolve_cron_secret()
        if secret is not None:
            auth_header = self.headers.get("authorization", "")
            if auth_header != f"Bearer {secret}":
                self._respond(401, {"error": "invalid cron secret"})
                return
        try:
            store = build_state_store()
            from oz_agent_sdk import OzAPI  # type: ignore[import-not-found]

            client = OzAPI(
                api_key=os.environ["WARP_API_KEY"],
                base_url=os.environ["WARP_API_BASE_URL"],
            )
            outcomes = run_cron_tick(
                store=store,
                retriever=client.agent.runs,
            )
        except Exception as exc:
            logger.exception("Cron tick aborted")
            self._respond(500, {"error": str(exc)})
            return
        self._respond(200, _summarize(outcomes))

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


__all__ = [
    "build_state_store",
    "build_workflow_handlers",
    "handler",
    "run_cron_tick",
]
