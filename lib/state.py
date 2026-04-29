"""In-flight run state, persisted in Vercel KV.

The webhook handler dispatches a cloud agent run and then returns
quickly so GitHub does not retry the delivery. The cron poller picks up
the run state on the next tick, polls Oz for terminal status, and
applies the result back to GitHub.

A run-state record carries:

- ``run_id``: Oz run identifier returned by ``client.agent.run``.
- ``workflow``: name from :mod:`control_plane.lib.routing`.
- ``repo``: ``owner/name`` slug.
- ``payload_subset``: the small slice of the webhook payload the cron
  poller needs to apply the result (issue/PR number, head/base refs,
  trigger source, etc.).
- ``dispatched_at``: ISO-8601 UTC timestamp.
- ``installation_id``: GitHub App installation id used to mint a token
  when the cron poller applies the result.

The store is intentionally storage-agnostic. The Vercel KV adapter
implements the protocol; the in-memory adapter is used in tests and
local ``vercel dev`` smoke runs.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol


# Vercel KV key namespace for in-flight runs. Concrete keys are
# `${RUN_STATE_KEY_PREFIX}${run_id}`.
RUN_STATE_KEY_PREFIX = "oz-control-plane:in-flight:"


@dataclass
class RunState:
    """Serialized in-flight run record.

    Stored as JSON in KV so the cron poller can fetch it without
    knowing the producer's Python version.
    """

    run_id: str
    workflow: str
    repo: str
    installation_id: int
    dispatched_at: float = field(default_factory=lambda: time.time())
    payload_subset: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    last_error: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "RunState":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("run state must decode to a JSON object")
        # Pull only known fields so an extra key in storage does not
        # crash the loader.
        return cls(
            run_id=str(data.get("run_id") or ""),
            workflow=str(data.get("workflow") or ""),
            repo=str(data.get("repo") or ""),
            installation_id=int(data.get("installation_id") or 0),
            dispatched_at=float(data.get("dispatched_at") or 0.0),
            payload_subset=dict(data.get("payload_subset") or {}),
            attempts=int(data.get("attempts") or 0),
            last_error=str(data.get("last_error") or ""),
        )


class StateStore(Protocol):
    """Tiny KV protocol implemented by the Vercel KV adapter and in-memory fake.

    Methods are typed only with the operations the dispatcher and cron
    poller actually use; do not extend this without trimming the
    in-memory adapter to match.
    """

    def put(self, key: str, value: str) -> None: ...
    def get(self, key: str) -> str | None: ...
    def delete(self, key: str) -> None: ...
    def keys(self, prefix: str) -> list[str]: ...


def _key_for(run_id: str) -> str:
    if not run_id:
        raise ValueError("run_id must be a non-empty string")
    return f"{RUN_STATE_KEY_PREFIX}{run_id}"


def save_run_state(store: StateStore, state: RunState) -> None:
    """Persist *state* keyed by ``state.run_id``."""
    store.put(_key_for(state.run_id), state.to_json())


def load_run_state(store: StateStore, run_id: str) -> RunState | None:
    """Return the run state for *run_id* or ``None`` when absent.

    Malformed records are dropped from the store so a corrupted entry
    cannot poison every cron tick.
    """
    raw = store.get(_key_for(run_id))
    if raw is None:
        return None
    try:
        return RunState.from_json(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        store.delete(_key_for(run_id))
        return None


def delete_run_state(store: StateStore, run_id: str) -> None:
    store.delete(_key_for(run_id))


def list_in_flight_runs(store: StateStore) -> Iterable[RunState]:
    """Yield every in-flight run state currently persisted."""
    for key in store.keys(RUN_STATE_KEY_PREFIX):
        raw = store.get(key)
        if raw is None:
            continue
        try:
            yield RunState.from_json(raw)
        except (ValueError, TypeError, json.JSONDecodeError):
            store.delete(key)


class InMemoryStateStore:
    """Simple ``dict``-backed :class:`StateStore` for tests.

    The Vercel KV adapter is provided by ``api/cron.py`` and
    ``api/webhook.py`` at import time; tests construct an instance of
    this fake instead.
    """

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def put(self, key: str, value: str) -> None:
        self._data[key] = value

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def keys(self, prefix: str) -> list[str]:
        return [key for key in self._data if key.startswith(prefix)]


__all__ = [
    "InMemoryStateStore",
    "RUN_STATE_KEY_PREFIX",
    "RunState",
    "StateStore",
    "delete_run_state",
    "list_in_flight_runs",
    "load_run_state",
    "save_run_state",
]
