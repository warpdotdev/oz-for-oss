"""Vercel serverless entrypoint for inbound GitHub webhooks.

Vercel's Python runtime invokes ``handler`` for each request to
``/api/webhook``. The handler:

1. Verifies the ``X-Hub-Signature-256`` header against the shared
   webhook secret using
   :mod:`control_plane.core.signatures`.
2. Decodes the JSON body and the GitHub event name from
   ``X-GitHub-Event``.
3. Asks :func:`control_plane.core.routing.route_event` which workflow
   should handle it.
4. Dispatches the cloud agent run, persists the in-flight run state,
   and returns 202 with the run identifier. GitHub state mutations are
   applied later by the cron poller so the webhook handler stays well
   within Vercel's per-request budget.

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
from typing import Any, Callable, Iterable, Mapping

from core.dispatch import (
    DispatchRequest,
    DispatchResult,
    PromptBuilder,
    dispatch_run,
    evaluate_route,
)
from core.routing import (
    RouteDecision,
    WORKFLOW_ACKNOWLEDGE_UNKNOWN_MENTION,
    WORKFLOW_ANNOUNCE_READY_ISSUE,
    WORKFLOW_PLAN_APPROVED,
    needs_triage_bot_author_allowlist,
    route_event,
)
from core.signatures import (
    SIGNATURE_HEADER,
    SignatureVerificationError,
    verify_signature,
)
from core.state import StateStore

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



def _run_synchronous_plan_approved(
    payload: Mapping[str, Any],
    *,
    sync_plan_approved: Callable[[Mapping[str, Any]], dict[str, Any] | None] | None,
) -> dict[str, Any] | None:
    """Run the synchronous ``plan-approved`` path inside the webhook.

    The handler posts the spec-approved comment, removes the
    ``ready-to-spec`` label from the linked issue, and decides whether
    a cloud-agent implementation run is needed.

    Returns the synchronous outcome (``{"action": "synced" | "skipped", ...}``)
    when no cloud agent is needed, or ``None`` to let the webhook fall
    through to the dispatch path.
    """
    if sync_plan_approved is None:
        return None
    return sync_plan_approved(payload)


def _run_synchronous_announce_ready_issue(
    payload: Mapping[str, Any],
    *,
    sync_announce_ready_issue: Callable[[Mapping[str, Any]], dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Run the synchronous ``announce-ready-issue`` path inside the webhook.

    The handler posts a one-shot availability-announcement comment on
    the labeled issue. There is no cloud-agent dispatch fallback —
    every routed delivery is fully handled inline — so the helper
    always returns a structured outcome dict (or ``None`` when the
    sync helper itself wasn't wired in, which only happens for unit
    tests that exercise pure routing).
    """
    if sync_announce_ready_issue is None:
        return None
    return sync_announce_ready_issue(payload)


def _run_synchronous_acknowledge_unknown_mention(
    payload: Mapping[str, Any],
    *,
    sync_acknowledge_unknown_mention: Callable[[Mapping[str, Any]], dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Run the synchronous ``acknowledge-unknown-mention`` path inside the webhook.

    The handler posts a one-shot acknowledgement comment on the PR
    telling the commenter that the mentioned handle was not recognized
    and pointing them at ``@oz-agent``. Like ``announce-ready-issue``
    there is no cloud-agent dispatch fallback, so the helper always
    returns a structured outcome dict (or ``None`` when the sync helper
    itself wasn't wired in, which only happens for unit tests that
    exercise pure routing).
    """
    if sync_acknowledge_unknown_mention is None:
        return None
    return sync_acknowledge_unknown_mention(payload)


def process_webhook_request(
    *,
    body: bytes,
    signature_header: str | None,
    event_header: str | None,
    delivery_id: str | None,
    secret: str,
    builder_registry: Mapping[str, PromptBuilder] | None = None,
    runner: Callable[..., Any] | None = None,
    config_factory: Callable[[str, str], Mapping[str, Any]] | None = None,
    store: StateStore | None = None,
    sync_plan_approved: Callable[[Mapping[str, Any]], dict[str, Any] | None] | None = None,
    sync_announce_ready_issue: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    sync_acknowledge_unknown_mention: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    triage_bot_author_allowlist: Iterable[str] | None = None,
    triage_bot_author_allowlist_loader: Callable[[Mapping[str, Any]], Iterable[str]] | None = None,
) -> WebhookResponse:
    """Validate a webhook delivery and dispatch the cloud agent run.

    The webhook handler completes the GitHub-facing work in a single
    request: it verifies the signature, routes the event, dispatches
    the cloud agent run (fire-and-forget), persists the in-flight
    record to KV, and returns 202. The cron poller (``api/cron.py``)
    drains the run on the next tick.

    The optional ``builder_registry`` / ``runner`` / ``config_factory``
    / ``store`` parameters are wired in by :class:`handler` from the
    Vercel environment; tests inject deterministic stubs.
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

    route_triage_bot_author_allowlist: frozenset[str] = frozenset(
        triage_bot_author_allowlist or ()
    )
    if (
        triage_bot_author_allowlist_loader is not None
        and needs_triage_bot_author_allowlist(event, payload)
    ):
        try:
            route_triage_bot_author_allowlist = frozenset(
                triage_bot_author_allowlist_loader(payload)
            )
        except Exception as exc:
            logger.exception("Failed to load triage bot author allowlist")
            return WebhookResponse(
                status=500,
                body={
                    "event": event,
                    "workflow": None,
                    "reason": "failed to load triage bot author allowlist",
                    "delivery": delivery_id or "",
                    "error": f"route config failed: {exc}",
                },
            )

    decision: RouteDecision = route_event(
        event,
        payload,
        triage_bot_author_allowlist=route_triage_bot_author_allowlist,
    )
    base_body: dict[str, Any] = {
        "event": event,
        "workflow": decision.workflow,
        "reason": decision.reason,
        "delivery": delivery_id or "",
    }

    # No workflow matched -> the route decision already explains why.
    if decision.workflow is None:
        return WebhookResponse(status=202, body=base_body)


    # ``plan-approved`` follows the same hybrid pattern: the
    # synchronous helper posts the spec-approved comment + removes
    # the ``ready-to-spec`` label, and only the rare
    # ``implementation-pending`` branch falls through to the dispatch
    # path. The sync helper mutates *payload* to stash the resolved
    # ``linked_issue_number`` so the dispatch builder reuses it.
    if decision.workflow == WORKFLOW_PLAN_APPROVED:
        try:
            outcome = _run_synchronous_plan_approved(
                payload, sync_plan_approved=sync_plan_approved
            )
        except Exception as exc:
            logger.exception("Synchronous plan-approved run failed")
            return WebhookResponse(
                status=500,
                body={**base_body, "error": f"plan-approved path failed: {exc}"},
            )
        if outcome is not None:
            return WebhookResponse(
                status=202,
                body={**base_body, "plan_approved": outcome},
            )

    # ``announce-ready-issue`` is fully synchronous: the webhook
    # posts a one-shot availability-announcement comment and never
    # dispatches a cloud agent. The sync helper always returns a
    # structured outcome so the response body carries the action /
    # reason for observability, and the dispatch path is skipped
    # entirely.
    if decision.workflow == WORKFLOW_ANNOUNCE_READY_ISSUE:
        try:
            outcome = _run_synchronous_announce_ready_issue(
                payload, sync_announce_ready_issue=sync_announce_ready_issue
            )
        except Exception as exc:
            logger.exception("Synchronous announce-ready-issue run failed")
            return WebhookResponse(
                status=500,
                body={**base_body, "error": f"announce-ready-issue path failed: {exc}"},
            )
        if outcome is None:
            # Sync helper not wired in (unit-test path that only
            # exercises routing); surface the routed decision and
            # return without dispatching.
            return WebhookResponse(status=202, body=base_body)
        return WebhookResponse(
            status=202,
            body={**base_body, "announce_ready_issue": outcome},
        )

    # ``acknowledge-unknown-mention`` is fully synchronous: the webhook
    # posts a one-shot comment letting the commenter know the mentioned
    # handle was not recognized and never dispatches a cloud agent. The
    # sync helper returns a structured outcome for observability; when it
    # is not wired in (pure-routing unit tests) we surface the routed
    # decision and skip the dispatch path entirely.
    if decision.workflow == WORKFLOW_ACKNOWLEDGE_UNKNOWN_MENTION:
        try:
            outcome = _run_synchronous_acknowledge_unknown_mention(
                payload,
                sync_acknowledge_unknown_mention=sync_acknowledge_unknown_mention,
            )
        except Exception as exc:
            logger.exception("Synchronous acknowledge-unknown-mention run failed")
            return WebhookResponse(
                status=500,
                body={
                    **base_body,
                    "error": f"acknowledge-unknown-mention path failed: {exc}",
                },
            )
        if outcome is None:
            return WebhookResponse(status=202, body=base_body)
        return WebhookResponse(
            status=202,
            body={**base_body, "acknowledge_unknown_mention": outcome},
        )

    if builder_registry is None or runner is None or config_factory is None or store is None:
        # The webhook handler is partially wired (e.g. unit tests that
        # only exercise routing). Keep returning 202 + reason so the
        # GitHub deliveries UI stays green.
        return WebhookResponse(status=202, body=base_body)

    try:
        request: DispatchRequest | None = evaluate_route(
            decision=decision,
            payload=payload,
            builder_registry=builder_registry,
        )
    except Exception as exc:
        logger.exception("Failed to evaluate route for delivery %s", delivery_id)
        return WebhookResponse(
            status=500,
            body={**base_body, "error": f"builder failed: {exc}"},
        )
    if request is None:
        return WebhookResponse(
            status=202,
            body={**base_body, "dispatched": False},
        )

    try:
        result: DispatchResult = dispatch_run(
            request=request,
            runner=runner,
            config_factory=config_factory,
            store=store,
        )
    except Exception as exc:
        logger.exception("Failed to dispatch run for delivery %s", delivery_id)
        return WebhookResponse(
            status=500,
            body={**base_body, "error": f"dispatch failed: {exc}"},
        )

    return WebhookResponse(
        status=202,
        body={
            **base_body,
            "dispatched": True,
            "run_id": result.run_id,
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

        # Lazy imports keep the test suite stdlib-only and let the
        # webhook function start cold without paying the import cost
        # for paths that do not need to dispatch. The wiring is built
        # per request because the builder registry needs a GitHub
        # client minted from the payload's installation id.
        try:
            wiring = _build_runtime_wiring(body=body)
        except Exception as exc:
            logger.exception("Webhook runtime wiring failed")
            self._respond(500, {"error": f"webhook runtime not ready: {exc}"})
            return
        response = process_webhook_request(
            body=body,
            signature_header=self.headers.get(SIGNATURE_HEADER),
            event_header=self.headers.get(_EVENT_HEADER),
            delivery_id=self.headers.get(_DELIVERY_HEADER),
            secret=secret,
            builder_registry=wiring["builder_registry"],
            runner=wiring["runner"],
            config_factory=wiring["config_factory"],
            store=wiring["store"],
            sync_plan_approved=wiring["sync_plan_approved"],
            sync_announce_ready_issue=wiring["sync_announce_ready_issue"],
            sync_acknowledge_unknown_mention=wiring[
                "sync_acknowledge_unknown_mention"
            ],
            triage_bot_author_allowlist_loader=wiring[
                "triage_bot_author_allowlist_loader"
            ],
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


def _build_runtime_wiring(*, body: bytes) -> dict[str, Any]:
    """Construct the production wiring (Oz SDK + KV + builders).

    Built per request so the builder registry can reuse a GitHub
    client minted with the payload's installation id. Imported lazily
    so the unit-test path (which exercises
    :func:`process_webhook_request` with stubs) does not need any of
    these dependencies on PYTHONPATH.
    """
    from oz_agent_sdk import OzAPI  # type: ignore[import-not-found]

    from api.cron import build_state_store
    from core.builders import build_builder_registry
    from core.github_app import fetch_installation_token
    from oz.oz_client import (  # type: ignore[import-not-found]
        build_agent_config,
    )
    from oz.workflow_config import (  # type: ignore[import-not-found]
        load_triage_bot_author_allowlist,
    )
    from workflows.acknowledge_unknown_mention import (  # type: ignore[import-not-found]
        apply_acknowledge_unknown_mention_sync,
    )
    from workflows.announce_ready_issue import (  # type: ignore[import-not-found]
        apply_announce_ready_issue_sync,
    )
    from workflows.plan_approved import (  # type: ignore[import-not-found]
        apply_plan_approved_sync,
    )

    import httpx
    from github import Auth, Github

    app_id = os.environ["OZ_GITHUB_APP_ID"]
    private_key = os.environ["OZ_GITHUB_APP_PRIVATE_KEY"]
    api_base = os.environ.get("GITHUB_API_BASE_URL", "https://api.github.com")

    class _HttpxClient:
        def post(self, url, *, headers, timeout):
            with httpx.Client(timeout=timeout) as client:
                return client.post(url, headers=headers)

    http = _HttpxClient()

    def _mint_github_client(installation_id: int) -> Github:
        token = fetch_installation_token(
            installation_id=installation_id,
            app_id=app_id,
            private_key=private_key,
            http=http,
            api_base=api_base,
        )
        return Github(auth=Auth.Token(token.token))

    # Decode the payload up front so the builder registry can mint
    # exactly one GitHub client per request, scoped to the payload's
    # installation id. The webhook re-decodes the body inside
    # ``process_webhook_request`` for signature verification, but the
    # JSON payload itself is small so the redundant decode is fine.
    try:
        payload_for_install = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload_for_install = {}
    payload_install_id = 0
    if isinstance(payload_for_install, dict):
        installation = payload_for_install.get("installation") or {}
        if isinstance(installation, dict):
            try:
                payload_install_id = int(installation.get("id") or 0)
            except (TypeError, ValueError):
                payload_install_id = 0

    cached_client: dict[str, Github] = {}

    def _client_for_payload() -> Github:
        if payload_install_id <= 0:
            raise RuntimeError(
                "webhook payload is missing installation.id; cannot mint a GitHub client"
            )
        if "client" not in cached_client:
            cached_client["client"] = _mint_github_client(payload_install_id)
        return cached_client["client"]

    builder_registry = build_builder_registry(
        github_client_factory=_client_for_payload,
    )

    sdk_client = OzAPI(
        api_key=os.environ["WARP_API_KEY"],
        base_url=os.environ["WARP_API_BASE_URL"],
    )

    def runner(*, prompt, title, config, skill, team, attachments=None):
        request = {
            "prompt": prompt,
            "title": title,
            "config": config,
            "team": team,
        }
        if skill:
            request["skill"] = skill
        if attachments:
            request["attachments"] = tuple(attachments)
        return sdk_client.agent.run(**request)

    from pathlib import Path as _Path

    def config_factory(config_name: str, role: str) -> Mapping[str, Any]:
        return build_agent_config(
            config_name=config_name,
            workspace=_Path("/tmp"),
            role=role,
        )


    def sync_plan_approved(
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        installation_id = int(
            (payload.get("installation") or {}).get("id") or 0
        )
        full_name = str(
            (payload.get("repository") or {}).get("full_name") or ""
        )
        if installation_id <= 0 or "/" not in full_name:
            return {
                "action": "skipped",
                "reason": "missing installation_id or repository.full_name",
            }
        client = _mint_github_client(installation_id)
        repo_handle = client.get_repo(full_name)
        return apply_plan_approved_sync(
            repo_handle, payload=payload, github_client=client
        )

    def sync_announce_ready_issue(
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        installation_id = int(
            (payload.get("installation") or {}).get("id") or 0
        )
        full_name = str(
            (payload.get("repository") or {}).get("full_name") or ""
        )
        if installation_id <= 0 or "/" not in full_name:
            return {
                "action": "skipped",
                "reason": "missing installation_id or repository.full_name",
            }
        client = _mint_github_client(installation_id)
        repo_handle = client.get_repo(full_name)
        return apply_announce_ready_issue_sync(
            repo_handle, payload=payload
        )

    def sync_acknowledge_unknown_mention(
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        installation_id = int(
            (payload.get("installation") or {}).get("id") or 0
        )
        full_name = str(
            (payload.get("repository") or {}).get("full_name") or ""
        )
        if installation_id <= 0 or "/" not in full_name:
            return {
                "action": "skipped",
                "reason": "missing installation_id or repository.full_name",
            }
        client = _mint_github_client(installation_id)
        repo_handle = client.get_repo(full_name)
        return apply_acknowledge_unknown_mention_sync(
            repo_handle, payload=payload
        )

    def triage_bot_author_allowlist_loader(payload: Mapping[str, Any]) -> frozenset[str]:
        full_name = str(
            (payload.get("repository") or {}).get("full_name") or ""
        )
        if "/" not in full_name:
            raise RuntimeError(
                "webhook payload is missing repository.full_name; "
                "cannot load triage bot author allowlist"
            )
        repo_handle = _client_for_payload().get_repo(full_name)
        workflow_root = _Path(__file__).resolve().parents[1]
        return load_triage_bot_author_allowlist(
            repo_handle,
            fallback_workspace=workflow_root,
        )

    return {
        "builder_registry": builder_registry,
        "runner": runner,
        "config_factory": config_factory,
        "store": build_state_store(),
        "sync_plan_approved": sync_plan_approved,
        "sync_announce_ready_issue": sync_announce_ready_issue,
        "sync_acknowledge_unknown_mention": sync_acknowledge_unknown_mention,
        "triage_bot_author_allowlist_loader": triage_bot_author_allowlist_loader,
    }


__all__ = ["WebhookResponse", "handler", "process_webhook_request"]
