"""matrix-nio, behind the transport contract.

The whole of shelltrix's knowledge of matrix-nio lives here: the client, the
callbacks it drives, the store, and the normalization of its objects into
`shelltrix.events`. `matrix_client.py` imports none of it, which is what makes
the transport replaceable — and what `test_no_module_above_the_transport_imports_nio`
enforces rather than leaves to memory.

The normalization lives here too, deliberately. `Room.user_names` already holds
disambiguated display names ("Alice", or "Alice (@bob:hs)" when two members share
one), so the UI never reproduces nio's naming rules and a second transport is not
asked to.
"""

from __future__ import annotations

from typing import BinaryIO

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

from .config import Credentials, decrypt_store, ensure_store_dir
from .events import ImageEvent, MessageEvent, MessagePage, Room
from .transport import POLL_MS, EventHooks, SendRefused, Transport, Upload


# ---------------------------------------------------------------------------
# nio -> shelltrix
# ---------------------------------------------------------------------------
def _to_room(room: MatrixRoom) -> Room:
    """Normalizes a nio room, resolving display names once and for all.

    `room.user_name()` is nio's own disambiguation: it returns
    "Alice (@bob:hs)" when two members share a name. We snapshot that here so the
    UI does not have to reproduce those rules — and so a Rust transport only has
    to produce a plain name→name mapping.
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


async def password_login(homeserver: str, user_id: str, password: str) -> Credentials:
    """Exchanges a password for a reusable session, and returns it.

    A module function rather than an operation of the transport, because it is
    the one step both backends share: the password is spent here, once, and the
    token it yields is what every later run restores. No store is written — the
    token is the whole deliverable — so this does not need an E2EE store to be
    decrypted first.

    A client is opened and closed rather than kept: the session that continues
    syncing is built from these credentials by the transport, not from this
    object.
    """
    client = AsyncClient(
        homeserver=homeserver, user=user_id, store_path=str(ensure_store_dir())
    )
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


class NioTransport(Transport):
    """The reference implementation of the contract, and the default backend."""

    name = "matrix-nio"

    def __init__(self, creds: Credentials, hooks: EventHooks) -> None:
        self.hooks = hooks
        self._store_loaded = False
        self._next_batch: str | None = None

        store_path = str(ensure_store_dir())
        self.client = AsyncClient(
            homeserver=creds.homeserver,
            user=creds.user_id,
            device_id=creds.device_id,
            store_path=store_path,
            config=AsyncClientConfig(
                store_sync_tokens=True,
                encryption_enabled=True,
            ),
        )
        self.client.access_token = creds.access_token
        self.client.user_id = creds.user_id

        self.client.add_event_callback(self._handle_message, RoomMessageText)
        self.client.add_event_callback(self._handle_image, RoomMessageImage)
        # The parent type, not the reaction class: a callback registered on
        # `RoomMessage` fires only for annotations, which carry `m.relates_to` as
        # `m.annotation`. Registering it on anything broader would announce every
        # message as a reaction.
        self.client.add_event_callback(self._handle_reaction, RoomMessage)
        self.client.add_event_callback(self._handle_typing, TypingNoticeEvent)
        self.client.add_event_callback(self._handle_invite, InviteMemberEvent)
        self.client.add_to_device_callback(
            self._handle_verification, (KeyVerificationEvent,)
        )

    # ------------------------------------------------------------------
    # The operations
    # ------------------------------------------------------------------
    async def send_event(
        self, room_id: str, message_type: str, content: dict, *, operation: str
    ) -> None:
        """Sends a room event, applying the security policy.

        Unverified devices are NOT ignored: in an encrypted room nio refuses the
        send (`LocalProtocolError`) rather than hand the message to a recipient
        whose device we cannot vouch for. That refusal is a `SendRefused` here,
        so the UI reports "not delivered" instead of the UI seeing a library
        exception type.
        """
        try:
            await self.client.room_send(
                room_id=room_id,
                message_type=message_type,
                content=content,
            )
        except LocalProtocolError as exc:
            raise SendRefused(str(exc)) from exc

    async def join(self, room_id_or_alias: str, *, operation: str) -> None:
        await self.client.join(room_id_or_alias)

    async def leave(self, room_id: str, *, operation: str) -> None:
        await self.client.room_leave(room_id)

    async def reactions(
        self, room_id: str, event_id: str, *, operation: str
    ) -> dict[str, str]:
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
            # Depending on the server and the age of the annotation, the relation
            # arrives either as `ReactionEvent` (`.key`) or as a `RoomMessageText`
            # with an empty body: we read both forms.
            key = getattr(event, "key", "") or ""
            if not key:
                content = getattr(event, "source", {}).get("content", {})
                _target, key = annotation_of(content)
            if key:
                by_sender[sender] = key
        return by_sender

    async def upload(
        self,
        file: BinaryIO,
        *,
        content_type: str,
        filename: str,
        filesize: int,
        encrypt: bool,
        operation: str,
    ) -> Upload:
        resp, decrypt_keys = await self.client.upload(
            file,
            content_type=content_type,
            filename=filename,
            encrypt=encrypt,
            filesize=filesize,
        )
        if not isinstance(resp, UploadResponse):
            # A successful request the server declined to store. Not an
            # exception in the network sense, so it gets the refusal wording.
            raise SendRefused(str(resp))
        return Upload(url=resp.content_uri, keys=dict(decrypt_keys))

    async def room_messages(
        self, room_id: str, *, start: str | None = None, limit: int = 50, operation: str
    ) -> MessagePage | None:
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

    async def start_verification(self, user_id: str, device_id: str, *, operation: str) -> None:
        await self.client.start_key_verification(user_id, device_id)

    async def confirm_sas(self, transaction_id: str, *, operation: str) -> None:
        await self.client.confirm_short_auth_string(transaction_id)

    async def cancel_sas(self, transaction_id: str, *, reject: bool, operation: str) -> None:
        await self.client.cancel_key_verification(transaction_id, reject=reject)

    async def logout(self, *, operation: str) -> None:
        """Invalidates the token server-side, then closes the client.

        Best-effort by design: an offline logout must still be able to clean up
        locally, and the local cleanup belongs to the facade, which runs it after
        this returns.
        """
        try:
            await self.client.logout()
        except Exception:
            pass  # even offline, we still clean up locally
        await self.client.close()

    async def close(self) -> None:
        await self.client.close()

    # ------------------------------------------------------------------
    # State the UI reads
    # ------------------------------------------------------------------
    def rooms(self) -> dict[str, Room]:
        return {rid: _to_room(room) for rid, room in self.client.rooms.items()}

    def next_batch(self) -> str | None:
        return getattr(self.client, "next_batch", None)

    def load_local_store(self) -> None:
        """Decrypts (if needed) then loads the local E2EE keys.

        Raises `decrypt_store()`'s `StoreLockedError` when the store key is
        unavailable: the UI then offers restoration via the session recovery key.
        """
        if self.client.olm is None or self._store_loaded:
            return
        # The store was encrypted at rest: restore it before reading the keys.
        decrypt_store()
        self.client.load_store()
        self._store_loaded = True
        # Read *after* the store is loaded: `store_sync_tokens` picks the token
        # up from disk, and a token from a previous run is what makes the first
        # sync incremental instead of a full one.
        self._next_batch = getattr(self.client, "next_batch", None)

    def supports_e2ee(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # The sync loop
    # ------------------------------------------------------------------
    async def _poll(self) -> None:
        await self.client.sync(
            timeout=POLL_MS,
            full_state=self._next_batch is None,
        )
        self._next_batch = getattr(self.client, "next_batch", None)

    def _synced(self) -> bool:
        # A `sync()` that returned is a sync that completed. There is nothing to
        # ask: matrix-nio reports the room list by calling back, not by handing
        # back a state.
        return True

    # ------------------------------------------------------------------
    # Internal callbacks (wired to nio)
    # ------------------------------------------------------------------
    async def _handle_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if self.hooks.on_message is not None:
            await self.hooks.on_message(
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
        if self.hooks.on_image is not None:
            await self.hooks.on_image(
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
        """Relays `m.reaction` events (emoji replies) to the UI."""
        if self.hooks.on_reaction is None:
            return
        content = getattr(event, "source", {}).get("content", {})
        relates = content.get("m.relates_to")
        if not isinstance(relates, dict) or relates.get("rel_type") != "m.annotation":
            return
        target = relates.get("event_id")
        key = relates.get("key")
        if not isinstance(target, str) or not isinstance(key, str) or not key:
            return
        await self.hooks.on_reaction(
            _to_room(room), target, key, getattr(event, "sender", "")
        )

    async def _handle_typing(self, room: MatrixRoom, event: TypingNoticeEvent) -> None:
        if self.hooks.on_typing is not None:
            await self.hooks.on_typing(room.room_id, event.users)

    async def _handle_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        # The hooks' `user_id`, not the transport's own: an invite is the one
        # event that has to know who we are, and reading that off a transport is
        # the kind of reach-through that only works on one of the two backends.
        if event.state_key != self.hooks.user_id:
            return
        if self.hooks.on_invite is not None:
            await self.hooks.on_invite(room.room_id, _to_room(room), event.sender)

    async def _handle_verification(self, event: KeyVerificationEvent) -> None:
        # "Short auth string" (SAS) verification: we NEVER accept and NEVER
        # confirm automatically. We display the emojis on screen and wait for an
        # explicit human confirmation before validating.
        if isinstance(event, KeyVerificationStart):
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is None:
                return
            await self.client.accept_key_verification(sas.transaction_id)
        elif isinstance(event, KeyVerificationKey):
            # The SAS is established: the emojis are now computable. We hand them
            # to the UI for comparison, without confirming.
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is None or self.hooks.on_sas_request is None:
                return
            device = sas.other_olm_device
            emojis = sas.get_emoji()
            await self.hooks.on_sas_request(
                sas.transaction_id, device.user_id, device.id, emojis
            )
        # KeyVerificationMac: nothing to do here. nio only verifies the device if
        # we have (already) validated the emojis via `confirm_sas`.