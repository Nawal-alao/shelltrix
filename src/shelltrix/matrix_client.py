"""The Matrix facade the whole UI talks to.

`ShelltrixClient` keeps its method signatures whatever library speaks Matrix
underneath: `widgets.py`, `screens/`, `dialogs/` and the timeline are written
against this class and nothing else. That is decision 3 of
`docs/decisions/0001-rust-core.md`, and it is what makes the core replaceable one
operation at a time.

So this module holds no Matrix library. Every protocol-shaped call goes through
`Transport`, and the one decision of which transport is made here, once, by
`_core.selected_backend()`:

- `nio_transport.NioTransport` — matrix-nio, the default and the only mandatory
  dependency. Implements everything.
- `rust_transport.RustTransport` — the compiled core, opt-in via
  `SHELLTRIX_CORE=rust`. Reads; does not yet send, and does not yet decrypt. Each
  missing operation refuses by name (`_core.UnsupportedOperation`) instead of
  failing on a client that was never built.

Nothing here changes shape when the backend does, which is the point: a refusal
and a success travel the same call, so the UI does not branch on the transport to
find out whether an operation exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import _core
from .config import Credentials, encrypt_store, remove_store
from .events import ImageEvent, MessageEvent, MessagePage, Room
from .nio_transport import NioTransport, password_login
from .rust_transport import RustTransport
from .transport import SendRefused, Transport

log = logging.getLogger(__name__)

MessageHandler = Callable[[Room, MessageEvent], Awaitable[None]]
ImageHandler = Callable[[Room, ImageEvent], Awaitable[None]]
TypingHandler = Callable[[str, list[str]], Awaitable[None]]
# Incoming invite: (room_id, room, inviter)
InviteHandler = Callable[[str, Room, str], Awaitable[None]]
# Emoji verification: (transaction_id, user_id, device_id, emojis)
# where emojis is a list of (emoji, description).
SasRequestHandler = Callable[[str, str, str, list[tuple[str, str]]], Awaitable[None]]
# Send failure (e.g. unverified devices in an encrypted room)
# (room_id, message)
SendErrorHandler = Callable[[str, str], Awaitable[None]]
# Incoming reaction: (room, target message event_id, emoji key, sender)
ReactionHandler = Callable[[Room, str, str, str], Awaitable[None]]
# First sync done: the UI can refresh its room list.
FirstSyncHandler = Callable[[], Awaitable[None]]


@dataclass
class ShelltrixClient:
    creds: Credentials
    #: The transport actually talking to the homeserver. Chosen once, in
    #: `__post_init__`, and the only place that chooses.
    transport: Transport = field(init=False)
    _sync_task: asyncio.Task | None = field(init=False, default=None)
    on_message: MessageHandler | None = None
    on_image: ImageHandler | None = None
    on_typing: TypingHandler | None = None
    on_invite: InviteHandler | None = None
    on_sas_request: SasRequestHandler | None = None
    on_send_error: SendErrorHandler | None = None
    on_reaction: ReactionHandler | None = None
    # First sync done: the UI then refreshes its room list (rooms only
    # arrive with the sync response, not at mount time).
    on_first_sync: FirstSyncHandler | None = None
    # True as soon as the first sync succeeded (a UI mounted later can read
    # the state instead of waiting for the callback).
    first_sync_done: bool = field(init=False, default=False)
    # State exposed to the UI (header):
    #   connecting → syncing → online | offline / reconnecting (backoff).
    sync_state: str = field(init=False, default="connecting")

    def __post_init__(self) -> None:
        self._backend = _core.selected_backend()

        if self._backend == "rust":
            self.transport = RustTransport(self.creds, self)
            if not self.transport.supports_e2ee():
                # Loud, because the alternative is invisible: without a crypto
                # store the core reports an encrypted room as empty, and a user
                # would read that as "nobody has said anything" rather than "this
                # client cannot read this room".
                log.warning(
                    "SHELLTRIX_CORE=rust but this build cannot decrypt encrypted "
                    "rooms: they will appear empty. Use SHELLTRIX_CORE=python for "
                    "E2EE, or build the core with --features e2e-encryption."
                )
            return

        self.transport = NioTransport(self.creds, self)

    # ------------------------------------------------------------------
    # Backend access
    # ------------------------------------------------------------------
    @property
    def backend(self) -> str:
        """Which transport this client is running on: `python` or `rust`."""
        return self._backend

    @property
    def client(self) -> Any:
        """The matrix-nio client, or None on the Rust backend.

        Not part of the interface the UI uses — it exists so a test can assert
        what the transport did with the library's own object. Reading it from
        anywhere else is the reach-through this whole design removes.
        """
        return self.transport.client

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    @staticmethod
    async def login(homeserver: str, user_id: str, password: str) -> Credentials:
        """Initial password login, produces reusable Credentials
        (access_token + device id) for subsequent runs.

        A module function of the nio transport, called from here so this module
        stays free of matrix-nio: the password is spent once, and the token it
        yields is what *both* backends restore on every later run.
        """
        return await password_login(homeserver, user_id, password)

    def load_local_store(self) -> None:
        """Decrypts (if needed) then loads the local E2EE keys.
        Raises decrypt_store()/StoreLockedError when the store key is
        unavailable: the UI then offers restoration via the session
        recovery key."""
        self.transport.load_local_store()

    def start(self) -> None:
        """Loads the local encryption keys and starts the sync loop.

        NEVER blocks: the first sync (full, to populate the room list) runs
        as a background task and the UI shows up immediately. The first
        sync measured on matrix.org took ~10 s; waiting for it here delayed
        the first render by just as much. The UI tracks progress via
        `sync_state` and refreshes on `on_first_sync`."""
        self.load_local_store()
        self.sync_state = "syncing"
        self._sync_task = asyncio.create_task(self._run_sync_forever())

    async def _fire_first_sync(self) -> None:
        """Tells the UI the first sync succeeded (room list is full).

        Isolated from the sync loop: a UI refresh error must not be mistaken
        for a network outage (backoff + "offline" status)."""
        if self.on_first_sync is None:
            return
        try:
            await self.on_first_sync()
        except Exception:
            pass

    async def _mark_first_sync(self) -> None:
        """Fires `on_first_sync` once, the first time the sync is known complete.

        Guarded on the flag rather than only on the call site because the loop
        can check after every poll and would otherwise fire it repeatedly — the
        UI repaints its whole room list every 30 seconds for nothing.
        """
        if self.first_sync_done:
            return
        self.first_sync_done = True
        await self._fire_first_sync()

    async def _run_sync_forever(self) -> None:
        """Runs the transport's loop, wired to this facade's state and handlers.

        The loop itself — backoff, reconnection, the first-sync announcement —
        lives in `Transport`, because both backends honour the same contract and
        two copies of it is how a fix ends up applying to only one.
        """
        await self.transport.run_sync_forever(
            on_state=self._set_state,
            on_synced=self._mark_first_sync,
        )

    def _set_state(self, state: str) -> None:
        self.sync_state = state

    async def stop(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
            # Awaited so the task is really finished before the transport goes
            # away: a cancelled loop still holding a `next_event` would wake into
            # a closed core and log a spurious error on the way out.
            with contextlib.suppress(asyncio.CancelledError):
                await self._sync_task
            self._sync_task = None
        await self.transport.close()
        # E2EE session keys are protected at rest after shutdown.
        # A key persistence failure is reported LOUDLY: we do not quit the
        # app leaving the store in plaintext without saying so.
        from .config import StoreEncryptionError

        try:
            encrypt_store()
        except StoreEncryptionError as exc:
            import sys

            print(
                f"[shelltrix] WARNING: {exc}\n"
                "E2EE session keys are NOT encrypted at rest. "
                "Check that a keyring backend is available.",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    async def _send(
        self, room_id: str, message_type: str, content: dict, *, operation: str = "sending a message"
    ) -> None:
        """Sends a room event, applying the security policy.

        `operation` is what the user was doing, so a refusal names it. Every send
        goes through here, which would otherwise mean all of them are reported as
        "sending a message".
        """
        try:
            await self.transport.send_event(
                room_id, message_type, content, operation=operation
            )
        except _core.UnsupportedOperation:
            # Refused for want of an implementation, not because the server said
            # no. Reporting it through `on_send_error` would dress a missing
            # feature up as a delivery failure, so it stays an exception.
            raise
        except SendRefused as exc:
            # The message genuinely was not delivered: an unverified device in an
            # encrypted room, or a server that declined it. The user is told.
            if self.on_send_error is not None:
                await self.on_send_error(room_id, str(exc))
        except Exception as exc:  # network / homeserver
            if self.on_send_error is not None:
                await self.on_send_error(room_id, f"{type(exc).__name__}: {exc}")

    async def send_message(
        self, room_id: str, body: str, *, reply_to_event_id: str = ""
    ) -> None:
        """Sends a text message, optionally as a reply to another one.

        When `reply_to_event_id` is given, we set BOTH forms the spec
        expects: the `m.in_reply_to` relation (read by modern clients) and
        the fallback prefix `<@author> original text` in the body (read by
        older clients, which only display the body).
        `reply_fallback` is the single place that builds that prefix.
        """
        content: dict = {"msgtype": "m.text", "body": body}
        if reply_to_event_id:
            content["m.relates_to"] = {
                "rel_type": "m.in_reply_to",
                "event_id": reply_to_event_id,
            }
        await self._send(room_id, "m.room.message", content, operation="sending a message")

    async def send_emote(self, room_id: str, body: str) -> None:
        """/me command: an action displayed in italics (* action name)."""
        await self._send(
            room_id,
            "m.room.message",
            {"msgtype": "m.emote", "body": body},
            operation="sending an emote",
        )

    async def react_to(self, room_id: str, event_id: str, reaction: str) -> None:
        """/react command: posts a reaction (annotation) on a message."""
        await self._send(
            room_id,
            "m.reaction",
            {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": event_id,
                    "key": reaction,
                }
            },
            operation="reacting to a message",
        )

    async def fetch_reactions(
        self, room_id: str, event_id: str
    ) -> dict[str, str]:
        """Re-reads a message's reactions via `/relations`.

        Safety net: the paginated history normally holds the annotations,
        but a message read from the cache (restart, offline) does not.
        This query is therefore the only way to get them back on demand.
        Returns `{sender: emoji}` — the canonical form of the index, which
        already deduplicates and applies emoji changes.

        A user only has one reaction per message: `/relations` returns the
        current state, not the removal history, which avoids having to count
        cancellations.
        """
        return await self.transport.reactions(
            room_id, event_id, operation="reading reactions"
        )

    async def part_room(self, room_id: str, message: str | None = None) -> None:
        """/quit command: leaves the room (optional farewell message)."""
        if message:
            await self.send_message(room_id, message)
        await self.transport.leave(room_id, operation="leaving a room")

    async def send_image(self, room_id: str, path: str) -> None:
        """/sendimg command: uploads an image and sends it (encrypted, like
        E2EE messages)."""
        file = Path(path).expanduser()
        if not file.is_file():
            if self.on_send_error is not None:
                await self.on_send_error(room_id, f"File not found: {path}")
            return
        mimetype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        size = file.stat().st_size
        try:
            with file.open("rb") as fh:
                upload = await self.transport.upload(
                    fh,
                    content_type=mimetype,
                    filename=file.name,
                    filesize=size,
                    encrypt=True,
                    operation="uploading an image",
                )
        except _core.UnsupportedOperation:
            raise  # a missing feature, not a failed upload
        except SendRefused as exc:
            if self.on_send_error is not None:
                await self.on_send_error(room_id, f"Upload refused: {exc}")
            return
        except Exception as exc:
            if self.on_send_error is not None:
                await self.on_send_error(room_id, f"Upload : {type(exc).__name__}: {exc}")
            return
        content = {
            "msgtype": "m.image",
            "body": file.name,
            "info": {"mimetype": mimetype, "size": size},
            "file": {**upload.keys, "url": upload.url},
        }
        await self._send(
            room_id, "m.room.message", content, operation="sending an image"
        )

    async def logout(self) -> None:
        """Logs out of the device: invalidates the token server-side, then
        erases the credentials and the local store.

        The erasure is last, and only happens if the transport could revoke
        first: deleting the local credentials while the token is still valid
        server-side would strand the session — the device keeps syncing and the
        user can neither log it out nor log in again as the same device.
        """
        await self.transport.logout(operation="logging out")
        self.creds.remove()
        remove_store()

    async def join_room(self, room_id_or_alias: str) -> None:
        await self.transport.join(room_id_or_alias, operation="joining a room")

    async def accept_invite(self, room_id: str) -> None:
        await self.transport.join(room_id, operation="accepting an invite")

    async def decline_invite(self, room_id: str) -> None:
        await self.transport.leave(room_id, operation="declining an invite")

    def rooms(self) -> dict[str, Room]:
        """Every room, named. Synchronous: the UI reads it on every repaint."""
        return self.transport.rooms()

    @property
    def user_id(self) -> str:
        """The logged-in user, as the UI needs it.

        The UI used to reach through `client.client.user_id` straight into
        nio. Exposing it here means nothing above this layer has to know
        which Matrix library is in use.
        """
        return self.creds.user_id

    @property
    def next_batch(self) -> str | None:
        """Sync pagination token for history backfill (or None before first sync)."""
        return self.transport.next_batch()

    async def room_messages(
        self, room_id: str, start: str | None = None, limit: int = 50
    ) -> MessagePage | None:
        """Fetches a room's history (scrollback) via pagination.

        `start` is a pagination token: None for the most recent messages,
        or a `prev_batch` token to go further back in time. Returns a
        `MessagePage` with normalized events, or None on error.
        """
        try:
            return await self.transport.room_messages(
                room_id, start=start, limit=limit, operation="reading history"
            )
        except _core.UnsupportedOperation:
            # The caller must know history is missing, not see "no messages":
            # `None` is read by the timeline as an empty room, which is a
            # different thing entirely.
            raise
        except Exception:
            return None

    async def verify_device_by_emoji(self, user_id: str, device_id: str) -> None:
        """Starts an interactive emoji verification with a given device.
        The SAS (Short Authentication String) is confirmed via
        `confirm_short_auth_string` once both sides see the same
        emojis.
        """
        await self.transport.start_verification(
            user_id, device_id, operation="verifying a device"
        )

    async def confirm_sas(self, transaction_id: str) -> None:
        """Confirms that the emojis match (human decision)."""
        await self.transport.confirm_sas(
            transaction_id, operation="confirming a verification"
        )

    async def reject_sas(self, transaction_id: str) -> None:
        """Cancels the verification: the SAS does not match."""
        await self.transport.cancel_sas(
            transaction_id, reject=True, operation="rejecting a verification"
        )

    async def cancel_sas(self, transaction_id: str) -> None:
        """Cancels the verification (user gave up)."""
        await self.transport.cancel_sas(
            transaction_id, reject=False, operation="cancelling a verification"
        )