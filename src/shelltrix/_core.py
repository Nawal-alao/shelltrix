"""Backend selection for the Matrix core.

The Python core (matrix-nio) stays the default and the only mandatory
dependency. The Rust core (`shelltrix-core`, a PyO3 extension) is opt-in and
installed separately: `SHELLTRIX_CORE=rust`.

This module is the seam between the two, and it is deliberately small. It
holds the *contract* of the migrated slices — see
`docs/decisions/0001-rust-core.md` — and nothing else.

Where the Rust core stands today: it **receives**. It syncs, classifies events
and resolves room names, which is everything `matrix_client.py` needs to display
a live conversation. It does not yet **send**, and it does not yet decrypt:
`supports_e2ee()` reports the second, and
[`UnsupportedOperation`] reports the first. So the honest position is a working
read-only client, not a replacement for matrix-nio — and the difference is
raised at the call rather than left to surface as a missing attribute.

Both backends implement `parse_sync_messages` with identical semantics, which
`tests/test_rust_core.py` verifies on the same payloads. Parity is what makes
the backend a free choice for the user.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Final, Mapping

from .events import ImageEvent, MessageEvent

log = logging.getLogger(__name__)

_BACKEND_ENV: Final = "SHELLTRIX_CORE"
_PYTHON: Final = "python"
_RUST: Final = "rust"

# How long `matrix_client`'s polling loop waits for one event before checking
# that it is still alive. Long enough that an idle client wakes rarely, short
# enough that a shutdown is not held up by it.
EVENT_WAIT_MS: Final = 30_000

try:  # optional, never a prerequisite
    import shelltrix_core as _rust
except ImportError:  # pragma: no cover - depends on the machine
    _rust = None


class UnsupportedOperation(NotImplementedError):
    """The selected core does not implement this operation yet.

    A `NotImplementedError`, so a caller that only cares that it failed does not
    have to know about the migration. It is raised *at the call*, naming the
    operation, because the alternative — an attribute error from a client that
    was never built — is a crash the user cannot act on.
    """


def _require_rust(operation: str) -> None:
    if _rust is None:
        raise RuntimeError(
            f"{operation} needs the Rust core, which is not installed "
            "(pip install shelltrix-core)"
        )


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
        _require_rust("a one-off login and sync")
    summary = _rust.login_and_sync(homeserver, user, password)
    return SyncSummary(
        user_id=summary.user_id,
        device_id=summary.device_id,
        joined_rooms=tuple(summary.joined_rooms),
    )


# The kinds the Rust stream reports. Named here rather than in `events.py`
# because these are transport labels: `events.py` describes what the UI shows,
# this describes what came off the wire.
KIND_MESSAGE: Final = "message"
KIND_IMAGE: Final = "image"
KIND_REACTION: Final = "reaction"
KIND_TYPING: Final = "typing"
KIND_INVITE: Final = "invite"


@dataclass(frozen=True)
class StreamEvent:
    """One event from the running sync loop, classified by the Rust core.

    Flat and tagged with `kind`, like `/sync` itself: a message fills five of
    the fields and leaves the rest at their defaults. `as_timeline_event`
    reshapes the two kinds the timeline renders.

    `source` is the raw event JSON, parsed back into Python here rather than
    handed over as text. The UI reads relations from it (`m.in_reply_to` on a
    reply), and that must keep working whatever the transport.
    """

    kind: str
    room_id: str
    sender: str = ""
    origin_server_ts: int = 0
    event_id: str = ""
    msgtype: str = "m.text"
    body: str = ""
    mentions: bool = False
    # Images: media URL, already resolved from `url` or `file.url`.
    url: str = ""
    # Reactions: the annotated event, and the emoji.
    target: str = ""
    key: str = ""
    # Typing: who is typing right now.
    users: tuple[str, ...] = ()
    source: Mapping[str, object] = field(default_factory=dict)

    def as_timeline_event(self) -> MessageEvent | ImageEvent | None:
        """The dataclasses of `events.py`, or None if this is not rendered.

        Only messages and images become timeline entries. Reactions, typing and
        invites have their own handlers, so returning None for them here is
        what keeps the UI from rendering a reaction as an empty bubble —
        which is exactly what reading messages without classifying does.
        """
        if self.kind == KIND_MESSAGE:
            return MessageEvent(
                room_id=self.room_id,
                sender=self.sender,
                body=self.body,
                event_id=self.event_id,
                server_timestamp=self.origin_server_ts or None,
                msgtype=self.msgtype,
                source=self.source,
            )
        if self.kind == KIND_IMAGE:
            return ImageEvent(
                room_id=self.room_id,
                sender=self.sender,
                body=self.body,
                event_id=self.event_id,
                server_timestamp=self.origin_server_ts or None,
                url=self.url,
                source=self.source,
            )
        return None


def start_sync(homeserver: str, user: str, password: str) -> SyncSummary:
    """Logs in and starts the sync loop in the background.

    Returns as soon as the loop is running; the caller drains it with
    [`next_event`]. This split is not a convenience: matrix-sdk's sync never
    returns, so the loop has to be owned by the transport as a task.
    """
    if _rust is None:
        _require_rust("syncing by password")
    summary = _rust.start_sync(homeserver, user, password)
    return SyncSummary(
        user_id=summary.user_id,
        device_id=summary.device_id,
        joined_rooms=tuple(summary.joined_rooms),
    )


def _to_stream_event(raw) -> StreamEvent:
    return StreamEvent(
        kind=raw.kind,
        room_id=raw.room_id,
        sender=raw.sender,
        origin_server_ts=raw.origin_server_ts,
        event_id=raw.event_id,
        msgtype=raw.msgtype or "m.text",
        body=raw.body,
        mentions=raw.mentions,
        url=raw.url,
        target=raw.target,
        key=raw.key,
        users=tuple(raw.users),
        source=json.loads(raw.source) if raw.source else {},
    )


def next_event(timeout_ms: int) -> StreamEvent | None:
    """Waits up to `timeout_ms` for the next event, or None if none arrives.

    None is the normal outcome in a quiet room, not a failure. A `RuntimeError`
    means the loop itself ended — the message says why, so the UI can report it
    instead of silently going quiet.
    """
    if _rust is None:
        _require_rust("the sync stream")
    event = _rust.next_event(timeout_ms)
    return None if event is None else _to_stream_event(event)


def stop_sync() -> None:
    """Stops the sync loop. Safe when none is running."""
    if _rust is not None:
        _rust.stop_sync()


@dataclass(frozen=True)
class RoomSnapshot:
    """One room, named, as the Rust core resolved it.

    `display_name` is never empty — the Rust side falls back to the room id — so
    a sidebar row can always be labelled.
    """

    room_id: str
    display_name: str
    name: str = ""
    user_names: Mapping[str, str] = field(default_factory=dict)


def start_sync_with_token(
    homeserver: str, user_id: str, device_id: str, access_token: str
) -> SyncSummary:
    """Restores a saved session and starts the sync loop in the background.

    The path the app takes on every run after the first: `Credentials` holds an
    access token and a device id, not a password. Logging in again instead would
    mint a new device per launch and eventually trip the homeserver's device
    limit.

    Raises:
        RuntimeError: if the token was refused, the homeserver is unreachable,
            or a sync is already running.
    """
    _require_rust("syncing with a saved session")
    summary = _rust.start_sync_with_token(homeserver, user_id, device_id, access_token)
    return SyncSummary(
        user_id=summary.user_id,
        device_id=summary.device_id,
        joined_rooms=tuple(summary.joined_rooms),
    )


def rooms_snapshot() -> tuple[RoomSnapshot, ...]:
    """The current room list, with display names resolved.

    Empty when no sync is running. Safe to call as often as the UI repaints: the
    Rust side recomputes only when a `/sync` has invalidated its cache.
    """
    _require_rust("listing rooms")
    return tuple(
        RoomSnapshot(
            room_id=room.room_id,
            display_name=room.display_name,
            name=room.name,
            user_names=dict(room.user_names),
        )
        for room in _rust.rooms_snapshot()
    )


def first_sync_done() -> bool:
    """Whether at least one `/sync` response has been processed.

    The UI needs this before listing rooms: rooms arrive with a sync response,
    never at construction time. Distinct from "an event arrived", because a
    quiet account produces none and would otherwise never be declared synced.
    """
    _require_rust("the sync state")
    return _rust.first_sync_done()


def supports_e2ee() -> bool:
    """Whether this build of the Rust core can decrypt encrypted rooms.

    False today. It is asked rather than assumed because the failure mode is
    silent in the worst way: without a crypto store an encrypted room is not
    reported as unreadable, it is reported as *empty*, which looks like a quiet
    conversation rather than a broken client.
    """
    return _rust is not None and _rust.supports_e2ee()


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