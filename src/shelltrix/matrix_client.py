"""Thin layer on top of matrix-nio for shelltrix.

This class centralizes everything touching the Matrix protocol: connection,
sync loop, message sending, basic encryption handling (E2EE) and emoji
verification. The Textual UI never talks directly to `nio` — it always
goes through here.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

CORE_BACKEND = os.getenv("SHELLTRIX_CORE", "python")

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    KeyVerificationEvent,
    KeyVerificationKey,
    KeyVerificationStart,
    LoginResponse,
    MatrixRoom,
    RoomMessage,
    RoomMessageImage,
    RoomMessageText,
    TypingNoticeEvent,
    UploadResponse,
)
from nio.exceptions import LocalProtocolError

from .config import Credentials, decrypt_store, encrypt_store, ensure_store_dir, remove_store
from .events import ImageEvent, MessageEvent, MessagePage, Room

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


# ---------------------------------------------------------------------------
# nio -> shelltrix
#
# This is the whole of shelltrix's knowledge of matrix-nio. Everything below
# runs once, at the boundary: the UI only ever sees the dataclasses above, so
# replacing nio with matrix-sdk means rewriting this section and nothing else.
# ---------------------------------------------------------------------------
def _to_room(room: MatrixRoom) -> Room:
    """Normalizes a nio room, resolving display names once and for all.

    `room.user_name()` is nio's own disambiguation: it returns
    "Alice (@bob:hs)" when two members share a name. We snapshot that here so
    the UI does not have to reproduce those rules — and so a Rust transport
    only has to produce a plain name→name mapping.
    """
    user_names: dict[str, str] = {}
    for user_id in room.users:
        name = room.user_name(user_id)
        if name is not None:
            user_names[user_id] = name
    return Room(
        room_id=room.room_id,
        display_name=room.display_name or room.room_id,
        name=room.name,
        user_names=user_names,
    )


def _to_timeline_event(
    room_id: str, event: RoomMessage
) -> MessageEvent | ImageEvent | None:
    """Normalizes a history event, or None if it is not something we render.

    History pages carry many event types; only messages and images become
    timeline entries, so anything else is dropped here rather than by the UI.
    """
    if isinstance(event, RoomMessageImage):
        return ImageEvent(
            room_id=room_id,
            sender=event.sender,
            body=event.body or "image",
            event_id=event.event_id,
            server_timestamp=event.server_timestamp,
            url=_image_url(event),
            source=getattr(event, "source", {}) or {},
        )
    if isinstance(event, RoomMessageText):
        return MessageEvent(
            room_id=room_id,
            sender=event.sender,
            body=event.body or "",
            event_id=event.event_id,
            server_timestamp=event.server_timestamp,
            msgtype=getattr(event, "msgtype", "m.text") or "m.text",
            source=getattr(event, "source", {}) or {},
        )
    return None


def _image_url(event: RoomMessageImage | RoomMessage) -> str:
    """Media URL of an image event, whichever shape the homeserver used.

    Matrix v3 serves `url`, older servers and encrypted uploads nest it under
    `file`. Resolving it here means the UI never sees that difference.
    """
    url = getattr(event, "url", "") or ""
    if url:
        return url
    file_info = getattr(event, "file", None)
    if isinstance(file_info, dict) and file_info.get("url"):
        return str(file_info["url"])
    content = getattr(event, "source", {}).get("content", {})
    if isinstance(content, dict):
        if content.get("url"):
            return str(content["url"])
        nested = content.get("file")
        if isinstance(nested, dict) and nested.get("url"):
            return str(nested["url"])
    return ""
# First sync done: the UI can refresh its room list.
FirstSyncHandler = Callable[[], Awaitable[None]]


@dataclass
class ShelltrixClient:
    creds: Credentials
    client: AsyncClient = field(init=False)
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
        if CORE_BACKEND == "rust":
            # Rust backend: the network and sync loop are provided by the
            # PyO3 extension (shelltrix_core). The nio client is not used.
            self.client = None
            self._sync_task = None
            self.first_sync_done = False
            self.sync_state = "connecting"
            # The Rust core will be driven externally (e.g. via start_sync / next_event).
            return
        store_path = str(ensure_store_dir())
        config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=True,
        )
        self.client = AsyncClient(
            homeserver=self.creds.homeserver,
            user=self.creds.user_id,
            device_id=self.creds.device_id,
            store_path=store_path,
            config=config,
        )
        self.client.access_token = self.creds.access_token
        self.client.user_id = self.creds.user_id
        self._store_loaded = False

        self.client.add_event_callback(self._handle_message, RoomMessageText)
        self.client.add_event_callback(self._handle_image, RoomMessageImage)
        self.client.add_event_callback(self._handle_reaction, RoomMessage)
        self.client.add_event_callback(self._handle_typing, TypingNoticeEvent)
        self.client.add_event_callback(self._handle_invite, InviteMemberEvent)
        self.client.add_to_device_callback(
            self._handle_verification, (KeyVerificationEvent,)
        )
        self._ever_connected = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    @staticmethod
    async def login(homeserver: str, user_id: str, password: str) -> Credentials:
        """Initial password login, produces reusable Credentials
        (access token + device id) for subsequent runs.
        """
        store_path = str(ensure_store_dir())
        client = AsyncClient(homeserver=homeserver, user=user_id, store_path=store_path)
        resp = await client.login(password, device_name="shelltrix")
        if not isinstance(resp, LoginResponse):
            await client.close()
            raise RuntimeError(f"Connection failed: {resp}")

        creds = Credentials(
            homeserver=homeserver,
            user_id=resp.user_id,
            device_id=resp.device_id,
            access_token=resp.access_token,
        )
        await client.close()
        return creds

    def load_local_store(self) -> None:
        """Decrypts (if needed) then loads the local E2EE keys.
        Raises decrypt_store()/StoreLockedError when the store key is
        unavailable: the UI then offers restoration via the session
        recovery key."""
        if self.client.olm is None or self._store_loaded:
            return
        # The store was encrypted at rest: we restore it before reading the
        # E2EE session keys.
        decrypt_store()
        self.client.load_store()
        self._store_loaded = True

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

    async def _run_sync_forever(self) -> None:
        """Sync loop as a background task, with automatic reconnection.

        matrix-nio does not reconnect on its own after a network outage: a
        `sync()` call that raises (network timeout, 5xx, 429, lost connection)
        would kill the task and leave the app "offline" forever.
        Here we retry with an exponential backoff (1s → 30s max) and reset
        it to zero as soon as a sync succeeds.
        """
        next_batch = getattr(self.client, "next_batch", None)
        delay = 1.0
        MAX_BACKOFF = 30.0
        first_sync_done = False
        while True:
            try:
                self.sync_state = "syncing"
                await self.client.sync(
                    timeout=30000,
                    full_state=next_batch is None,
                )
                next_batch = getattr(self.client, "next_batch", None)
                self.sync_state = "online"
                self._ever_connected = True
                if not first_sync_done:
                    first_sync_done = True
                    self.first_sync_done = True
                    await self._fire_first_sync()
                delay = 1.0  # success: reset the backoff clock to zero
                # Yield to the event loop between two iterations: avoids a
                # tight loop if the server replies instantly.
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Network / server outage: report the state, then wait.
                # "reconnecting" if we were already online (backoff running),
                # otherwise "offline" (never connected at startup).
                if self._ever_connected:
                    self.sync_state = "reconnecting"
                else:
                    self.sync_state = "offline"
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF)

    async def stop(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
        await self.client.close()
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
    async def _send(self, room_id: str, message_type: str, content: dict) -> None:
        """Sends a room event, applying the security policy.

        Unverified devices are NOT ignored: if the room is encrypted and a
        contact has not verified their device, nio refuses the send
        (LocalProtocolError). We report it to the UI instead of delivering to
        a potentially compromised recipient.
        """
        try:
            await self.client.room_send(
                room_id=room_id,
                message_type=message_type,
                content=content,
            )
        except LocalProtocolError as exc:
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
        await self._send(room_id, "m.room.message", content)

    async def send_emote(self, room_id: str, body: str) -> None:
        """/me command: an action displayed in italics (* action name)."""
        await self._send(
            room_id,
            "m.room.message",
            {"msgtype": "m.emote", "body": body},
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
        from nio.api import RelationshipType

        from .formatting import annotation_of

        by_sender: dict[str, str] = {}
        async for event in self.client.room_get_event_relations(
            room_id,
            event_id,
            rel_type=RelationshipType.annotation,
        ):
            sender = getattr(event, "sender", "") or ""
            if not sender:
                continue
            # Depending on the server and the age of the annotation, the
            # relation arrives either as `ReactionEvent` (`.key`) or as a
            # `RoomMessageText` with an empty body: we read both forms.
            key = getattr(event, "key", "") or ""
            if not key:
                content = getattr(event, "source", {}).get("content", {})
                _target, key = annotation_of(content)
            if key:
                by_sender[sender] = key
        return by_sender

    async def part_room(self, room_id: str, message: str | None = None) -> None:
        """/quit command: leaves the room (optional farewell message)."""
        if message:
            await self.send_message(room_id, message)
        await self.client.room_leave(room_id)

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
                resp, decrypt_keys = await self.client.upload(
                    fh,
                    content_type=mimetype,
                    filename=file.name,
                    encrypt=True,
                    filesize=size,
                )
        except Exception as exc:
            if self.on_send_error is not None:
                await self.on_send_error(room_id, f"Upload : {type(exc).__name__}: {exc}")
            return
        if not isinstance(resp, UploadResponse):
            if self.on_send_error is not None:
                await self.on_send_error(room_id, f"Upload refused: {resp}")
            return
        content = {
            "msgtype": "m.image",
            "body": file.name,
            "info": {"mimetype": mimetype, "size": size},
            "file": {**decrypt_keys, "url": resp.content_uri},
        }
        await self._send(room_id, "m.room.message", content)

    async def logout(self) -> None:
        """Logs out of the device: invalidates the token server-side, then
        erases the credentials and the local store."""
        try:
            await self.client.logout()
        except Exception:
            pass  # even offline, we still clean up locally
        await self.client.close()
        self.creds.remove()
        remove_store()

    async def join_room(self, room_id_or_alias: str) -> None:
        await self.client.join(room_id_or_alias)

    async def accept_invite(self, room_id: str) -> None:
        await self.client.join(room_id)

    async def decline_invite(self, room_id: str) -> None:
        await self.client.room_leave(room_id)

    def rooms(self) -> dict[str, Room]:
        return {rid: _to_room(room) for rid, room in self.client.rooms.items()}

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
        return getattr(self.client, "next_batch", None)

    async def room_messages(self, room_id: str, start: str | None = None, limit: int = 50):
        """Fetches a room's history (scrollback) via pagination.

        `start` is a pagination token: None for the most recent messages,
        or a `prev_batch` token to go further back in time. Returns a
        `MessagePage` with normalized events, or None on error.
        """
        try:
            from nio import RoomMessagesResponse

            resp = await self.client.room_messages(room_id, start=start, limit=limit)
            if not isinstance(resp, RoomMessagesResponse):
                return None
            events: list[MessageEvent | ImageEvent] = []
            for ev in resp.chunk:
                normalized = _to_timeline_event(room_id, ev)
                if normalized is not None:
                    events.append(normalized)
            return MessagePage(events=events, start=resp.start, end=resp.end)
        except Exception:
            return None

    async def verify_device_by_emoji(self, user_id: str, device_id: str) -> None:
        """Starts an interactive emoji verification with a given device.
        The SAS (Short Authentication String) is confirmed via
        `confirm_short_auth_string` once both sides see the same
        emojis.
        """
        await self.client.start_key_verification(user_id, device_id)

    async def confirm_sas(self, transaction_id: str) -> None:
        """Confirms that the emojis match (human decision)."""
        await self.client.confirm_short_auth_string(transaction_id)

    async def reject_sas(self, transaction_id: str) -> None:
        """Cancels the verification: the SAS does not match."""
        await self.client.cancel_key_verification(transaction_id, reject=True)

    async def cancel_sas(self, transaction_id: str) -> None:
        """Cancels the verification (user gave up)."""
        await self.client.cancel_key_verification(transaction_id, reject=False)

    # ------------------------------------------------------------------
    # Internal callbacks (wired to nio)
    # ------------------------------------------------------------------
    async def _handle_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if self.on_message is not None:
            await self.on_message(
                _to_room(room),
                MessageEvent(
                    room_id=room.room_id,
                    sender=event.sender,
                    body=event.body or "",
                    event_id=event.event_id,
                    server_timestamp=event.server_timestamp,
                    msgtype=getattr(event, "msgtype", "m.text") or "m.text",
                    source=getattr(event, "source", {}) or {},
                ),
            )

    async def _handle_image(self, room: MatrixRoom, event: RoomMessageImage) -> None:
        if self.on_image is not None:
            await self.on_image(
                _to_room(room),
                ImageEvent(
                    room_id=room.room_id,
                    sender=event.sender,
                    body=event.body or "image",
                    event_id=event.event_id,
                    server_timestamp=event.server_timestamp,
                    url=_image_url(event),
                    source=getattr(event, "source", {}) or {},
                ),
            )

    async def _handle_reaction(self, room: MatrixRoom, event: RoomMessage) -> None:
        """Relays `m.reaction` events (emoji replies) to the UI.

        We listen on the parent type `RoomMessage` rather than the dedicated
        class: that way the callback only fires on annotations, which carry
        `m.relates_to` as `m.annotation` — otherwise we would be notified of
        ALL messages.
        """
        if self.on_reaction is None:
            return
        content = getattr(event, "source", {}).get("content", {})
        relates = content.get("m.relates_to")
        if not isinstance(relates, dict) or relates.get("rel_type") != "m.annotation":
            return
        target = relates.get("event_id")
        key = relates.get("key")
        if not isinstance(target, str) or not isinstance(key, str) or not key:
            return
        await self.on_reaction(
            _to_room(room), target, key, getattr(event, "sender", "")
        )

    async def _handle_typing(self, room: MatrixRoom, event: TypingNoticeEvent) -> None:
        if self.on_typing is not None:
            await self.on_typing(room.room_id, event.users)

    async def _handle_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        if event.state_key != self.client.user_id:
            return
        if self.on_invite is not None:
            await self.on_invite(room.room_id, _to_room(room), event.sender)

    async def _handle_verification(self, event: KeyVerificationEvent) -> None:
        # "Short auth string" (SAS) verification: we NEVER accept and
        # NEVER confirm automatically. We display the emojis on screen and
        # wait for an explicit human confirmation before validating.
        if isinstance(event, KeyVerificationStart):
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is None:
                return
            await self.client.accept_key_verification(sas.transaction_id)
        elif isinstance(event, KeyVerificationKey):
            # The SAS is established: the emojis are now computable.
            # We pass them to the UI for comparison, without confirming.
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is None or self.on_sas_request is None:
                return
            device = sas.other_olm_device
            emojis = sas.get_emoji()
            await self.on_sas_request(
                sas.transaction_id, device.user_id, device.id, emojis
            )
        # KeyVerificationMac: nothing to do here. nio only verifies the
        # device if we have (already) validated the emojis via confirm_sas().
