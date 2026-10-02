"""Backend selection for the Matrix core.

The Python core (matrix-nio) stays the default and the only mandatory
dependency. The Rust core (`shelltrix-core`, a PyO3 extension) is opt-in and
installed separately: `SHELLTRIX_CORE=rust`.

This module is the seam between the two, and it is deliberately small. It
holds the *contract* of the first migrated slice — see
`docs/decisions/0001-rust-core.md` — and nothing else. It is not wired into
`matrix_client.py` yet, and that is not an oversight: the client consumes
events through matrix-nio callbacks (`add_event_callback`), while a Rust core
built on matrix-sdk emits a stream. Bridging the two is a change of
architecture, not a substitution of a function call, so it will be done as
one reviewed step rather than half-wired.

Both backends implement `parse_sync_messages` with identical semantics, which
`tests/test_rust_core.py` verifies on the same payloads. Parity is what makes
the backend a free choice for the user.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Final

log = logging.getLogger(__name__)

_BACKEND_ENV: Final = "SHELLTRIX_CORE"
_PYTHON: Final = "python"
_RUST: Final = "rust"

try:  # optional, never a prerequisite
    import shelltrix_core as _rust
except ImportError:  # pragma: no cover - depends on the machine
    _rust = None


@dataclass(frozen=True)
class SyncMessage:
    """One `m.room.message` of a `/sync` response.

    Mirrors `shelltrix_core.SyncMessage` field for field; the parity test
    compares them attribute by attribute.
    """

    sender: str
    origin_server_ts: int
    event_id: str
    msgtype: str
    body: str
    mentions: bool


@dataclass(frozen=True)
class SyncSummary:
    """What one `/sync` told us, as the Rust transport saw it.

    Mirrors `shelltrix_core.SyncSummary`. Plain data on purpose: this is the
    shape the facade can hand to the UI without importing matrix-sdk.
    """

    user_id: str
    device_id: str
    joined_rooms: tuple[str, ...]


def login_and_sync(homeserver: str, user: str, password: str) -> SyncSummary:
    """Logs in to `homeserver` and runs a single `/sync`, through the Rust core.

    Blocking: it waits on the network, so callers must run it off the event
    loop (`asyncio.to_thread`), exactly as they already do for matrix-nio's
    calls. The Rust side releases the GIL while it waits, so the Textual UI
    keeps repainting meanwhile — `tests/test_rust_core.py` asserts that,
    because it is the failure mode that would look like an unrelated hang.

    Raises:
        RuntimeError: if the homeserver is unreachable, the credentials are
            refused, or `/sync` fails.
    """
    if _rust is None:
        raise RuntimeError("the Rust core is not installed (pip install shelltrix-core)")
    summary = _rust.login_and_sync(homeserver, user, password)
    return SyncSummary(
        user_id=summary.user_id,
        device_id=summary.device_id,
        joined_rooms=tuple(summary.joined_rooms),
    )


@dataclass(frozen=True)
class StreamEvent:
    """One `m.room.message` from the running sync loop."""

    room_id: str
    sender: str
    origin_server_ts: int
    event_id: str
    msgtype: str
    body: str
    mentions: bool


def start_sync(homeserver: str, user: str, password: str) -> SyncSummary:
    """Logs in and starts the sync loop in the background.

    Returns as soon as the loop is running; the caller drains it with
    [`next_event`]. This split is not a convenience: matrix-sdk's sync never
    returns, so the loop has to be owned by the transport as a task.
    """
    if _rust is None:
        raise RuntimeError("the Rust core is not installed (pip install shelltrix-core)")
    summary = _rust.start_sync(homeserver, user, password)
    return SyncSummary(
        user_id=summary.user_id,
        device_id=summary.device_id,
        joined_rooms=tuple(summary.joined_rooms),
    )


def next_event(timeout_ms: int) -> StreamEvent | None:
    """Waits up to `timeout_ms` for the next event, or None if none arrives.

    None is the normal outcome in a quiet room, not a failure. A `RuntimeError`
    means the loop itself ended — the message says why, so the UI can report it
    instead of silently going quiet.
    """
    if _rust is None:
        raise RuntimeError("the Rust core is not installed (pip install shelltrix-core)")
    event = _rust.next_event(timeout_ms)
    if event is None:
        return None
    return StreamEvent(
        room_id=event.room_id,
        sender=event.sender,
        origin_server_ts=event.origin_server_ts,
        event_id=event.event_id,
        msgtype=event.msgtype,
        body=event.body,
        mentions=event.mentions,
    )


def stop_sync() -> None:
    """Stops the sync loop. Safe when none is running."""
    if _rust is not None:
        _rust.stop_sync()


def rust_available() -> bool:
    """Whether the compiled core is importable on this machine."""
    return _rust is not None


def selected_backend() -> str:
    """The active backend name, `python` or `rust`.

    An unknown value falls back to `python` with a warning: a typo in an
    environment variable must not cost the user their client. A missing Rust
    build falls back too — the core is an implementation detail, never a
    prerequisite — but loudly, since the user asked for something they cannot
    get.
    """
    choice = os.environ.get(_BACKEND_ENV, _PYTHON).strip().lower()
    if choice == _PYTHON:
        return _PYTHON
    if choice != _RUST:
        log.warning(
            "%s=%r is not a valid core backend (expected %r or %r), using %r",
            _BACKEND_ENV, choice, _PYTHON, _RUST, _PYTHON,
        )
        return _PYTHON
    if _rust is None:
        log.warning(
            "%s=%s but the Rust core is not installed (pip install "
            "shelltrix-core); falling back to %r",
            _BACKEND_ENV, _RUST, _PYTHON,
        )
        return _PYTHON
    return _RUST


def parse_sync_messages(payload: bytes, *, backend: str | None = None) -> list[SyncMessage]:
    """Extract the `m.room.message` events of the joined rooms of a `/sync`.

    Args:
        payload: the raw response, as received.
        backend: force a backend instead of honouring the environment. Used by
            the parity test; `None` means "whatever the user configured".

    Raises:
        ValueError: if the payload is not JSON, or carries no `rooms` object.
            Both backends raise the same type for the same input.
    """
    chosen = backend or selected_backend()
    if chosen == _RUST:
        if _rust is None:
            raise ValueError(f"{_RUST} core requested but not installed")
        return [
            SyncMessage(
                sender=m.sender,
                origin_server_ts=m.origin_server_ts,
                event_id=m.event_id,
                msgtype=m.msgtype,
                body=m.body,
                mentions=m.mentions,
            )
            for m in _rust.parse_sync_messages(payload)
        ]
    return _parse_sync_messages_python(payload)


def _parse_sync_messages_python(payload: bytes) -> list[SyncMessage]:
    """The reference implementation, and the one that defines the contract.

    Deliberately plain: no matrix-nio, no cache, no display names. If this and
    the Rust core agree on every field, the two are interchangeable.
    """
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid /sync JSON: {exc}") from exc

    rooms = decoded.get("rooms")
    joined = rooms.get("join") if isinstance(rooms, dict) else None
    if not isinstance(joined, dict):
        raise ValueError("no `rooms.join` object in /sync")

    out: list[SyncMessage] = []
    for room in joined.values():
        if not isinstance(room, dict):
            continue
        timeline = room.get("timeline")
        events = timeline.get("events") if isinstance(timeline, dict) else None
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict) or event.get("type") != "m.room.message":
                continue
            content = event.get("content")
            if not isinstance(content, dict):
                continue
            mentions = content.get("m.mentions")
            user_ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
            out.append(
                SyncMessage(
                    sender=event.get("sender") or "",
                    origin_server_ts=event.get("origin_server_ts") or 0,
                    event_id=event.get("event_id") or "",
                    msgtype=content.get("msgtype") or "m.text",
                    body=content.get("body") or "",
                    mentions=isinstance(user_ids, list) and bool(user_ids),
                )
            )
    return out