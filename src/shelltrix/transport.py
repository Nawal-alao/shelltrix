"""What shelltrix asks of a Matrix library, independent of which one.

`ShelltrixClient` is the facade the UI talks to, and it holds no Matrix library of
its own. Everything protocol-shaped sits behind `Transport`: matrix-nio today
(`nio_transport.NioTransport`), the compiled Rust core as it grows
(`rust_transport.RustTransport`).

Two consequences, and they are the reason this module exists:

- Migrating one operation means writing one method on one transport. Before the
  seam, every unmigrated operation reached for `self.client` and died with
  `AttributeError: 'NoneType' object has no attribute 'room_send'` on the Rust
  backend — a client that looked alive and failed on first use.
- An operation a transport has not implemented refuses by name, at the call, so
  the user gets "sending is not available on the Rust core yet, unset
  SHELLTRIX_CORE to go back to matrix-nio" instead of a traceback.

The sync loop lives here too, because reconnection is the same contract on both
backends: keep one `/sync` alive forever with exponential backoff, report the
state, announce the first completed sync once. Only `_poll` differs — one long
poll, however the transport happens to spell it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, BinaryIO, Callable, Final, Mapping, Protocol

from ._core import UnsupportedOperation
from .events import ImageEvent, MessageEvent, MessagePage, Room

#: Longest the loop waits between two attempts at a homeserver that is down.
#: Short enough that a network coming back is noticed, long enough that a laptop
#: in a tunnel does not spend its battery on it.
MAX_BACKOFF: Final = 30.0

#: How long one long poll lasts, matching matrix-nio's `timeout=30000`. The
#: homeserver answers this long before holding the connection, so it is the unit
#: at which a healthy-but-quiet connection is checked.
POLL_MS: Final = 30_000


class SendRefused(RuntimeError):
    """The message was not sent, and nothing is wrong with the network.

    Either the homeserver declined it, or the crypto policy did — an encrypted
    room with an unverified device, where delivering would hand the message to a
    possibly compromised recipient. Distinct from an exception for a missing
    implementation, which must never be reported as a delivery failure: that
    would tell the user their message was rejected when nothing was sent.
    """


@dataclass(frozen=True)
class Upload:
    """What a media upload produced, in the shape the Matrix spec defines.

    `keys` is the encrypted-upload envelope (`key`, `iv`, `hashes`, `sha256`).
    The spec fixes it, not the library, so a transport fills it in and the
    facade builds the event content without knowing who uploaded.
    """

    url: str
    keys: Mapping[str, object] = field(default_factory=dict)


class EventHooks(Protocol):
    """The handlers a transport reports into.

    Attributes rather than methods, because a transport reads them *at call
    time*: the UI assigns them after the client is built
    (`screens/chat.py:on_mount`), so a transport that snapshotted them at
    construction would capture `None` forever and deliver nothing.
    """

    @property
    def user_id(self) -> str:
        """Who we are, which an invite is the only event that needs to know.

        Read rather than taken from a transport: reading an account off the
        library's own object is the kind of reach-through that only works on one
        of the two backends.
        """
        ...

    on_message: Callable[[Room, MessageEvent], Awaitable[None]] | None
    on_image: Callable[[Room, ImageEvent], Awaitable[None]] | None
    on_typing: Callable[[str, list[str]], Awaitable[None]] | None
    on_invite: Callable[[str, Room, str], Awaitable[None]] | None
    on_reaction: Callable[[Room, str, str, str], Awaitable[None]] | None
    on_sas_request: Callable[[str, str, str, list[tuple[str, str]]], Awaitable[None]] | None


class RestartSync(Exception):
    """The transport rebuilt whatever it owns; retry the loop immediately.

    Raised by `_poll`, caught by `run_sync_forever`, and never seen outside a
    transport. Not an error: matrix-nio's loop recovers by calling `sync()`
    again, while the Rust core owns its own sync task and has to be restarted
    from scratch. Both are reconnections, so neither should pay a backoff for it.
    """


class Transport:
    """Base of the two backends, and the whole of what they must implement.

    Every operation refuses here, so a subclass implements what it can actually
    do and a caller can never reach a half-built transport. Refusing is the
    default rather than an `abstractmethod` because the two backends are at
    different stages, not because implementations are optional.

    `operation` is a keyword on the operations the facade calls with a specific
    label, and it appears verbatim in the refusal. That string is the only part
    of the failure a user can act on, so the facade owns the wording: "sending an
    emote" is what they were doing, "operation not implemented" is not.
    """

    #: The underlying library's client, when there is one. `None` on the Rust
    #: backend. Nothing outside a transport reads this — it exists so a test can
    #: assert what the transport did with it.
    client: Any = None

    #: How this backend is named in a refusal, since that string is the part of
    #: the failure a user reads.
    name: str = "selected backend"

    def _unmigrated(self, operation: str) -> UnsupportedOperation:
        """The refusal for an operation this backend does not have.

        Short on purpose. An earlier version also listed what else was missing,
        which was true when it was written and a lie the moment the next
        operation landed — "sending still needs migrating" on a core that has
        just learned to send is worse than saying nothing.
        """
        return UnsupportedOperation(
            f"{operation} is not available on {self.name} yet. "
            "Unset SHELLTRIX_CORE to go back to matrix-nio."
        )

    # ------------------------------------------------------------------
    # The operations
    # ------------------------------------------------------------------
    async def send_event(
        self, room_id: str, message_type: str, content: dict, *, operation: str
    ) -> None:
        """Posts one event to a room. Text, emote, reaction, reply."""
        raise self._unmigrated(operation)

    async def join(self, room_id_or_alias: str, *, operation: str) -> None:
        """Joins a room by id or alias, which is also how an invite is accepted."""
        raise self._unmigrated(operation)

    async def leave(self, room_id: str, *, operation: str) -> None:
        """Leaves a room, which is also how an invite is declined."""
        raise self._unmigrated(operation)

    async def reactions(self, room_id: str, event_id: str, *, operation: str) -> dict[str, str]:
        """The current reactions on a message, as `{sender: emoji}`.

        The current state rather than the removal history, so an emoji changed or
        cancelled needs no counting.
        """
        raise self._unmigrated(operation)

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
        """Uploads a binary object to the homeserver's media repository."""
        raise self._unmigrated(operation)

    async def room_messages(
        self, room_id: str, *, start: str | None = None, limit: int = 50, operation: str
    ) -> MessagePage | None:
        """A page of history. `None` on failure, never an empty page.

        The distinction is load-bearing: an empty page is a room with no history
        in it, and the timeline cannot tell the user which one it is looking at.
        A transport that cannot answer at all must raise instead.
        """
        raise self._unmigrated(operation)

    async def start_verification(self, user_id: str, device_id: str, *, operation: str) -> None:
        """Begins an interactive emoji verification with another device."""
        raise self._unmigrated(operation)

    async def confirm_sas(self, transaction_id: str, *, operation: str) -> None:
        """Confirms that both sides see the same emojis. Never automatic."""
        raise self._unmigrated(operation)

    async def cancel_sas(self, transaction_id: str, *, reject: bool, operation: str) -> None:
        """Ends a verification: refused by the user, or abandoned."""
        raise self._unmigrated(operation)

    async def logout(self, *, operation: str) -> None:
        """Invalidates this device's access token, server-side.

        Must be refused, not emulated, by a transport that cannot revoke: the
        facade erases the local credentials afterwards, and doing that to a token
        that is still valid leaves a session that keeps syncing and cannot be
        logged out again.
        """
        raise self._unmigrated(operation)

    async def close(self) -> None:
        """Shuts the transport down. Safe when nothing was ever started."""

    # ------------------------------------------------------------------
    # State the UI reads
    # ------------------------------------------------------------------
    def rooms(self) -> dict[str, Room]:
        """Every room, named. Synchronous: the sidebar reads it on every repaint."""
        return {}

    def next_batch(self) -> str | None:
        """The `/sync` pagination token, or None when there is none.

        The sidebar prints it, so inventing one is worse than admitting there is
        none.
        """
        return None

    def load_local_store(self) -> None:
        """Loads the at-rest encrypted session keys, if this backend has any."""

    def supports_e2ee(self) -> bool:
        """Whether this backend can read encrypted rooms.

        Asked rather than assumed: without a crypto store an encrypted room is
        not reported as unreadable, it is reported as *empty*, which reads as a
        quiet conversation rather than a broken client.
        """
        return False

    # ------------------------------------------------------------------
    # The sync loop
    # ------------------------------------------------------------------
    async def run_sync_forever(
        self,
        *,
        on_state: Callable[[str], None],
        on_synced: Callable[[], Awaitable[None]],
    ) -> None:
        """Keeps one `/sync` alive, forever, with exponential backoff.

        Shared by both backends. It was duplicated once per transport, which is
        how a reconnection fix ends up applying to only one of them.

        `on_state` is `syncing` → `online`, or `offline` before the first success
        and `reconnecting` after one, for the header. `on_synced` fires once,
        after the first sync that actually completed.
        """
        delay = 1.0
        ever_connected = False
        announced = False
        # Announced once, not per poll: the header reads this state, and
        # re-announcing "syncing" before every long poll makes it flicker to
        # "online" and back every 30 seconds on a perfectly healthy connection.
        on_state("syncing")
        while True:
            try:
                await self._poll()
                on_state("online")
                ever_connected = True
                # Asked of the transport rather than inferred from "an event
                # arrived": an account nobody has spoken in produces no event, and
                # would never be declared synced.
                if not announced and self._synced():
                    announced = True
                    await on_synced()
                delay = 1.0
                # Hands the loop back between two iterations: without it a
                # homeserver that answers instantly turns the sync into a spin.
                await asyncio.sleep(0)
            except RestartSync:
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                if ever_connected:
                    on_state("reconnecting")
                else:
                    on_state("offline")
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF)

    async def _poll(self) -> None:
        """One long poll: returns when the homeserver answered, raises if it did not.

        Returns normally on an *empty* answer as well — a quiet room is a
        healthy connection, and treating it as a failure would make a busy client
        back off for being busy.
        """
        raise NotImplementedError

    def _synced(self) -> bool:
        """Whether at least one `/sync` response has completed."""
        raise NotImplementedError