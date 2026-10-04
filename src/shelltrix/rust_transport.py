"""The compiled Rust core, behind the transport contract.

Where the core stands: it **receives**. It restores the session, runs the sync
loop, classifies events and resolves room names — everything
`matrix_client.py` needs to display a live conversation. It does not yet send,
and it does not yet decrypt.

So every operation the core has not migrated refuses by name, and
`supports_e2ee()` reports the second. The honest position is a working read-only
client, not a replacement for matrix-nio, and the difference is raised at the
call rather than left to surface as a missing attribute.

The shape differs from the nio backend in one place that matters: matrix-nio's
loop recovers by calling `sync()` again, while the core owns its own sync task.
When its queue reports the loop ended, this transport tears it down and starts a
new one — same contract, same backoff, one different step.
"""

from __future__ import annotations

import asyncio
import logging

from . import _core
from ._core import KIND_IMAGE, KIND_INVITE, KIND_MESSAGE, KIND_REACTION, KIND_TYPING
from .config import Credentials
from .events import ImageEvent, MessageEvent, Room
from .transport import POLL_MS, EventHooks, RestartSync, Transport

log = logging.getLogger(__name__)


class RustTransport(Transport):
    """Drains the Rust core's event queue and dispatches to the handlers.

    The mirror image of matrix-nio's `add_event_callback` registrations: the UI
    sees the same callbacks with the same arguments either way, so this is the
    whole of the transport swap on the receiving side.

    The core is faked in tests. That is deliberate: a test needing a homeserver is
    a test nobody runs, and the failure this guards against — a client running on
    a transport that was never really built — is exactly the kind that survives
    without one.
    """

    name = "the Rust core"

    def __init__(self, creds: Credentials, hooks: EventHooks) -> None:
        self.hooks = hooks
        self._creds = creds
        #: The room list, as the last snapshot named it. The core keeps its own;
        #: this is the UI's copy, refreshed before it is read.
        self._rooms: dict[str, Room] = {}

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    async def close(self) -> None:
        _core.stop_sync()

    def load_local_store(self) -> None:
        """Nothing to load.

        The core has no crypto store yet, which is why `supports_e2ee()` is False
        and the app warns at startup. Doing nothing here is correct rather than a
        stub: there is simply nothing to read.
        """

    def supports_e2ee(self) -> bool:
        return _core.supports_e2ee()

    def next_batch(self) -> str | None:
        """Always None.

        The core does not expose its `/sync` token, and `room_messages` raises
        rather than paging. The sidebar shows no token there, which is accurate
        rather than misleading.
        """
        return None

    # ------------------------------------------------------------------
    # Rooms
    # ------------------------------------------------------------------
    def rooms(self) -> dict[str, Room]:
        self._refresh_rooms()
        return dict(self._rooms)

    def room(self, room_id: str) -> Room:
        """The room an event came from, named.

        Falls back to the room id so an event is never dropped for want of a name
        — but tries the snapshot first, because an event can arrive in the same
        sync response that first names its room.
        """
        room = self._rooms.get(room_id)
        if room is None:
            self._refresh_rooms()
            room = self._rooms.get(room_id)
        if room is None:
            room = Room(room_id=room_id, display_name=room_id)
            self._rooms[room_id] = room
        return room

    def _refresh_rooms(self) -> None:
        """Rebuilds the room cache from the core's snapshot.

        Called before the UI reads `rooms()`, and whenever an event arrives for a
        room the cache has not seen — the core may already know a name that the
        last snapshot predated.
        """
        try:
            snapshot = _core.rooms_snapshot()
        except RuntimeError as exc:
            # No sync running (stopping, or a failed start). An empty room list
            # is the right answer: showing stale rooms after a disconnect would
            # be worse, and this is not worth failing a repaint over.
            log.debug("cannot read the room list from the Rust core: %s", exc)
            return
        self._rooms = {
            room.room_id: Room(
                room_id=room.room_id,
                display_name=room.display_name,
                name=room.name,
                user_names=room.user_names,
            )
            for room in snapshot
        }

    # ------------------------------------------------------------------
    # The sync loop
    # ------------------------------------------------------------------
    def _session(self) -> dict[str, str]:
        """The saved session, as `start_sync_with_token` wants it."""
        return {
            "homeserver": self._creds.homeserver,
            "user_id": self._creds.user_id,
            "device_id": self._creds.device_id,
            "access_token": self._creds.access_token,
        }

    async def _poll(self) -> None:
        try:
            # Blocking: runs in a worker thread, and the core releases the GIL
            # while it waits, so the UI keeps repainting (which
            # `tests/test_rust_core.py` asserts).
            event = await asyncio.to_thread(_core.next_event, POLL_MS)
        except RuntimeError:
            # The core's sync task ended: the homeserver dropped us, or the token
            # was revoked. Either way the queue is closed and only a fresh sync
            # can reopen it. A failure *to* start propagates, so a homeserver that
            # is down still backs off instead of spinning.
            _core.stop_sync()
            await asyncio.to_thread(_core.start_sync_with_token, **self._session())
            raise RestartSync from None
        # `None` means the long poll expired: the loop is healthy and the room is
        # simply quiet, which is the common case.
        if event is not None:
            await self.dispatch(event)

    def _synced(self) -> bool:
        return _core.first_sync_done()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    async def dispatch(self, event: _core.StreamEvent) -> None:
        """Routes one classified event to the handler that owns its kind.

        The `isinstance` checks are not ceremony: `as_timeline_event` returns
        `None` for a kind that is not rendered, and the classifier and the
        normalizer disagreeing would otherwise hand the timeline a `None` where
        a `MessageEvent` is expected. Dropping the event is the lesser failure.
        """
        room = self.room(event.room_id)
        if event.kind == KIND_MESSAGE:
            normalized = event.as_timeline_event()
            if isinstance(normalized, MessageEvent) and self.hooks.on_message:
                await self.hooks.on_message(room, normalized)
        elif event.kind == KIND_IMAGE:
            normalized = event.as_timeline_event()
            if isinstance(normalized, ImageEvent) and self.hooks.on_image:
                await self.hooks.on_image(room, normalized)
        elif event.kind == KIND_REACTION and self.hooks.on_reaction is not None:
            await self.hooks.on_reaction(room, event.target, event.key, event.sender)
        elif event.kind == KIND_TYPING and self.hooks.on_typing is not None:
            await self.hooks.on_typing(event.room_id, list(event.users))
        elif event.kind == KIND_INVITE and self.hooks.on_invite is not None:
            await self.hooks.on_invite(event.room_id, room, event.sender)