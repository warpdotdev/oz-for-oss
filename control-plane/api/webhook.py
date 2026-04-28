"""Vercel serverless entrypoint for inbound GitHub webhooks.

Vercel's Python runtime invokes ``handler`` for each request to
``/api/webhook``. The handler:

1. Verifies the ``X-Hub-Signature-256`` header against the shared
   webhook secret using
   :mod:`control_plane.lib.signatures`.
2. Decodes the JSON body and the GitHub event name from
   ``X-GitHub-Event``.
3. Asks :func:`control_plane.lib.routing.route_event` which workflow
   should handle it.
4. Returns 202 immediately. The actual cloud agent dispatch and
   GitHub state mutations happen in the cron poller so the webhook
   handler stays well within Vercel's per-request budget.

The handler is a thin BaseHTTPRequestHandler subclass to match the
shape Vercel's Python runtime expects. Unit tests exercise the routing
+ signature plumbing through :func:`process_webhook_request` directly,
which avoids the HTTP plumbing entirely.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Any

from lib.routing import RouteDecision, route_event
from lib.signatures import (
    SIGNATURE_HEADER,
    SignatureVerificationError,
    verify_signature,
)

logger = logging.getLogger(__name__)

# Header GitHub uses to communicate the event name. Lowercased so the
# handler can do a case-insensitive lookup against the dictionary
# returned by ``BaseHTTPRequestHandler.headers``.
_EVENT_HEADER = "x-github-event"
_DELIVERY_HEADER = "x-github-delivery"


@dataclass(frozen=True)
class WebhookResponse:
    """Structured response surfaced by :func:`process_webhook_request`."""

    status: int
    body: dict[str, Any]


def _resolve_secret() -> str:
    secret = os.environ.get("OZ_GITHUB_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "OZ_GITHUB_WEBHOOK_SECRET is not configured for this Vercel "
            "deployment. Webhooks cannot be verified."
        )
    return secret


def process_webhook_request(
    *,
    body: bytes,
    signature_header: str | None,
    event_header: str | None,
    delivery_id: str | None,
    secret: str,
) -> WebhookResponse:
    """Validate a webhook delivery and return the response body.

    The webhook handler is intentionally cheap: it returns 202 as soon
    as the request is accepted. The cron poller is responsible for the
    long-running dispatch + apply work. Returning 202 (rather than 200)
    makes the role explicit to the GitHub deliveries UI.
    """
    try:
        verify_signature(secret=secret, body=body, signature_header=signature_header)
    except SignatureVerificationError as exc:
        logger.warning("Rejected webhook delivery %s: %s", delivery_id, exc)
        return WebhookResponse(status=401, body={"error": "invalid signature"})

    if not isinstance(event_header, str) or not event_header.strip():
        return WebhookResponse(
            status=400,
            body={"error": "missing X-GitHub-Event header"},
        )
    event = event_header.strip().lower()

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return WebhookResponse(
            status=400,
            body={"error": f"invalid JSON body: {exc}"},
        )
    if not isinstance(payload, dict):
        return WebhookResponse(
            status=400,
            body={"error": "webhook payload must be a JSON object"},
        )

    decision: RouteDecision = route_event(event, payload)
    return WebhookResponse(
        status=202,
        body={
            "event": event,
            "workflow": decision.workflow,
            "reason": decision.reason,
            "delivery": delivery_id or "",
        },
    )


class handler(BaseHTTPRequestHandler):  # noqa: N801 - Vercel requires this exact symbol name.
    """Vercel-compatible request handler.

    Vercel's Python runtime expects a class named ``handler`` in the
    module-level namespace. The class extends
    :class:`BaseHTTPRequestHandler` and routes POST requests to
    :func:`process_webhook_request`.
    """

    server_version = "OzForOSSWebhook/1.0"

    def do_POST(self) -> None:  # noqa: N802 - signature comes from BaseHTTPRequestHandler.
        try:
            secret = _resolve_secret()
        except RuntimeError as exc:
            logger.error("%s", exc)
            self._respond(500, {"error": str(exc)})
            return
        length = int(self.headers.get("content-length", "0") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        response = process_webhook_request(
            body=body,
            signature_header=self.headers.get(SIGNATURE_HEADER),
            event_header=self.headers.get(_EVENT_HEADER),
            delivery_id=self.headers.get(_DELIVERY_HEADER),
            secret=secret,
        )
        self._respond(response.status, response.body)

    def do_GET(self) -> None:  # noqa: N802 - intentional override for readiness probes.
        # Vercel cron jobs hit ``/api/cron`` directly, so this endpoint
        # only needs a tiny readiness probe for monitoring.
        self._respond(200, {"status": "ok"})

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


__all__ = ["WebhookResponse", "handler", "process_webhook_request"]
