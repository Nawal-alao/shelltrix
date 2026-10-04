"""Tests for the Rust backend as `ShelltrixClient` uses it.

The point of these is the wiring, not the transport: the core's own behaviour is
covered by `test_rust_core.py` and by the Rust tests, and needs a homeserver.
What is testable here — and what was silently broken — is that the client
actually *runs* on the Rust backend, routes each event to the right handler, and
refuses the operations the core has not migrated.

So the core is faked. That is deliberate: a test that needs a homeserver is a
test nobody runs, and the bug being guarded against (a client with no transport
behind it, dying on first use) is exactly the kind that survives without one.

`SHELLTRIX_CORE=rust` selects the backend in `_core.selected_backend()`, so the
client is built the way a user would build it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from shelltrix import _core
from shelltrix._core import (
    KIND_IMAGE,
    KIND_INVITE,
    KIND_MESSAGE,
    KIND_REACTION,
    KIND_TYPING,
    RoomSnapshot,
    StreamEvent,
    UnsupportedOperation,
)
from shelltrix.config import Credentials
from shelltrix.events import ImageEvent, MessageEvent, Room
from shelltrix.matrix_client import ShelltrixClient
from shelltrix.nio_transport import NioTransport
from shelltrix.rust_transport import RustTransport
from shelltrix.transport import Transport

creds = Credentials(
    homeserver="https://hs.example",
    user_id="@me:hs.example",
    device_id="SHELLTRIX",
    access_token="syt_token",
)

#: Any real file. `send_image` checks the path before it reaches the transport,
#: so this reaches the refusal instead of the "no such file" path — which is
#: also worth being true, but is tested where it belongs.
IMAGE = __file__


# ---------------------------------------------------------------------------
# A stand-in for the compiled core
# ---------------------------------------------------------------------------
@dataclass
class FakeCore:
    """Records what the client asks of the core, and answers from a script.

    `events` is consumed in order by `next_event`; `None` means "the long poll
    expired", which the client must treat as healthy rather than as a failure.
    Once exhausted, `next_event` raises, which is how the core reports that its
    loop ended — the client is expected to start a new one.
    """

    events: list[StreamEvent | None] = field(default_factory=list)
    rooms: list[RoomSnapshot] = field(default_factory=list)
    synced: bool = False
    #: How many times the client asked the core to (re)start a sync.
    starts: int = 0
    stops: int = 0
    #: Set to an exception to make `start_sync_with_token` fail that many times.
    start_failures: int = 0
    #: Set to an exception to make `next_event` fail that many times.
    poll_failures: int = 0
    #: Every `next_event` call, so a test can assert the loop keeps polling.
    polls: int = 0

    def install(self, monkeypatch) -> "FakeCore":
        for name in (
            "next_event",
            "start_sync_with_token",
            "stop_sync",
            "rooms_snapshot",
            "first_sync_done",
            "supports_e2ee",
        ):
            monkeypatch.setattr(_core, name, getattr(self, f"core_{name}"))
        monkeypatch.setattr(_core, "selected_backend", lambda: "rust")
        return self

    # -- the core's surface, as `matrix_client` calls it --
    def core_next_event(self, timeout_ms: int):
        self.polls += 1
        if self.poll_failures:
            self.poll_failures -= 1
            raise RuntimeError("the sync loop stopped: connection refused")
        if not self.events:
            raise RuntimeError("the sync loop stopped")
        event = self.events.pop(0)
        # Both an event and an expired long poll mean the homeserver answered a
        # /sync: an empty response is a successful one, and the real core marks
        # the first sync done on it. Getting this wrong would leave the room list
        # blank forever on an account nobody has spoken in.
        self.synced = True
        return event

    def core_start_sync_with_token(self, **session):
        self.starts += 1
        if self.start_failures:
            self.start_failures -= 1
            raise RuntimeError("cannot build a client")
        self.synced = True
        return _core.SyncSummary(
            user_id=session.get("user_id", ""),
            device_id=session.get("device_id", ""),
            joined_rooms=tuple(r.room_id for r in self.rooms),
        )

    def core_stop_sync(self) -> None:
        self.stops += 1
        self.synced = False

    def core_rooms_snapshot(self):
        return tuple(self.rooms)

    def core_first_sync_done(self) -> bool:
        return self.synced

    def core_supports_e2ee(self) -> bool:
        return True


def message(
    body: str = "hello", *, room_id: str = "!r:hs", sender: str = "@bob:hs"
) -> StreamEvent:
    return StreamEvent(
        kind=KIND_MESSAGE,
        room_id=room_id,
        sender=sender,
        origin_server_ts=1_700_000_000_000,
        event_id=f"${body}",
        body=body,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def test_the_rust_backend_builds_a_client_without_a_transport(fake_core):
    client = ShelltrixClient(creds=creds)
    assert client.backend == "rust"
    # Not a fake-but-present nio client: genuinely absent, and nothing has
    # dereferenced it yet.
    assert client.client is None


def test_the_python_backend_still_builds_an_nio_client(monkeypatch):
    """The default backend must be untouched by all of the above.

    A test on the Rust path alone would still pass if it broke matrix-nio, which
    is the backend every user without a Rust build actually gets.
    """
    from unittest.mock import MagicMock, patch

    monkeypatch.setattr(_core, "selected_backend", lambda: "python")
    with patch("shelltrix.nio_transport.AsyncClient") as cls:
        cls.return_value.add_event_callback = MagicMock()
        cls.return_value.add_to_device_callback = MagicMock()
        client = ShelltrixClient(creds=creds)
    assert client.backend == "python"
    assert client.client is not None
    assert client.rooms() == {}


def test_both_backends_implement_the_same_operations():
    """matrix-nio is the complete reference; the Rust one is a subset of it.

    A method present on one transport and forgotten on the other fails silently,
    because `Transport` supplies a default for most of them: a `rooms()` that was
    never overridden shows up as an empty sidebar rather than as an error. The
    operations themselves refuse loudly; only these do not.
    """
    contract = {
        name
        for name, value in vars(Transport).items()
        if callable(value) and not name.startswith("__")
    }
    rust_only = {
        name for name in contract if name in vars(RustTransport)
    } - set(vars(NioTransport))
    assert rust_only == set(), (
        f"the Rust transport implements {sorted(rust_only)}, which the complete "
        "reference does not: either the operation is not part of the contract, "
        "or matrix-nio is missing it too"
    )


@pytest.mark.asyncio
async def test_start_creates_a_polling_task(fake_core):
    client = ShelltrixClient(creds=creds)
    client.start()
    assert client._sync_task is not None
    assert not client._sync_task.done()


# ---------------------------------------------------------------------------
# The operations that are not migrated
# ---------------------------------------------------------------------------
# The regression these guard: `16527e3` set `self.client = None` for the Rust
# backend while every method still reached for it. Nothing tested that path, so
# `SHELLTRIX_CORE=rust` "worked" — right up to the first message, which raised
# `AttributeError: 'NoneType' object has no attribute 'room_send'`.
UNSUPPORTED_CALLS = [
    ("send_message", lambda c: c.send_message("!r:hs", "hi"), "sending a message"),
    ("send_emote", lambda c: c.send_emote("!r:hs", "waves"), "sending an emote"),
    (
        "react_to",
        lambda c: c.react_to("!r:hs", "$1", "\N{THUMBS UP SIGN}"),
        "reacting to a message",
    ),
    ("send_image", lambda c: c.send_image("!r:hs", IMAGE), "uploading an image"),
    ("fetch_reactions", lambda c: c.fetch_reactions("!r:hs", "$1"), "reading reactions"),
    ("part_room", lambda c: c.part_room("!r:hs"), "leaving a room"),
    ("join_room", lambda c: c.join_room("#a:hs"), "joining a room"),
    ("accept_invite", lambda c: c.accept_invite("!r:hs"), "accepting an invite"),
    ("decline_invite", lambda c: c.decline_invite("!r:hs"), "declining an invite"),
    ("room_messages", lambda c: c.room_messages("!r:hs"), "reading history"),
    (
        "verify_device_by_emoji",
        lambda c: c.verify_device_by_emoji("@bob:hs", "DEV"),
        "verifying a device",
    ),
    ("confirm_sas", lambda c: c.confirm_sas("tx"), "confirming a verification"),
    ("reject_sas", lambda c: c.reject_sas("tx"), "rejecting a verification"),
    ("cancel_sas", lambda c: c.cancel_sas("tx"), "cancelling a verification"),
    ("logout", lambda c: c.logout(), "logging out"),
]

UNSUPPORTED_IDS = [name for name, _, _ in UNSUPPORTED_CALLS]


@pytest.mark.parametrize(
    ("call", "expected"), [(c, e) for _, c, e in UNSUPPORTED_CALLS], ids=UNSUPPORTED_IDS
)
@pytest.mark.asyncio
async def test_an_unmigrated_operation_says_so_instead_of_crashing(
    fake_core, call, expected
):
    client = ShelltrixClient(creds=creds)
    with pytest.raises(UnsupportedOperation) as caught:
        await call(client)
    message = str(caught.value)
    # The message has to name what the user was doing and the way out: a user
    # who reads "reading history is not available on the Rust backend yet ...
    # Unset SHELLTRIX_CORE" can act on it; an AttributeError cannot.
    assert expected in message
    assert "SHELLTRIX_CORE" in message


@pytest.mark.asyncio
async def test_a_refused_send_is_not_reported_as_a_delivery_failure(fake_core):
    """`on_send_error` means "the server would not accept this".

    Dressing a missing implementation up as a delivery failure would tell the
    user their message was rejected when nothing was ever sent.
    """
    reported: list[tuple[str, str]] = []

    async def on_send_error(room_id: str, message: str) -> None:
        reported.append((room_id, message))

    client = ShelltrixClient(creds=creds)
    client.on_send_error = on_send_error
    with pytest.raises(UnsupportedOperation):
        await client.send_message("!r:hs", "hi")
    assert reported == []


@pytest.mark.asyncio
async def test_history_is_refused_rather_than_reported_as_empty(fake_core):
    """`room_messages` returns None on failure, which reads as "no messages".

    History that cannot be fetched must not be mistaken for a room that has
    none, so the refusal travels out instead.
    """
    client = ShelltrixClient(creds=creds)
    with pytest.raises(UnsupportedOperation):
        await client.room_messages("!r:hs")


@pytest.mark.asyncio
async def test_logout_does_not_delete_credentials_it_could_not_revoke(fake_core, monkeypatch):
    """`logout` erases the local credentials once the token is invalidated.

    The Rust core cannot invalidate it, so it must refuse *before* erasing
    anything: otherwise the token stays valid server-side, the local copy is
    gone, and the user can neither sync nor log out again.
    """
    erased: list[str] = []
    monkeypatch.setattr(creds, "remove", lambda: erased.append("creds"))
    monkeypatch.setattr("shelltrix.matrix_client.remove_store", lambda: erased.append("store"))

    client = ShelltrixClient(creds=creds)
    with pytest.raises(UnsupportedOperation):
        await client.logout()
    assert erased == []


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def test_rooms_comes_from_the_core_snapshot(fake_core):
    fake_core.rooms = [
        RoomSnapshot(room_id="!a:hs", display_name="Planning", name="Planning",
                     user_names={"@bob:hs": "Bob"}),
        RoomSnapshot(room_id="!b:hs", display_name="Bob", name=""),
    ]
    client = ShelltrixClient(creds=creds)
    rooms = client.rooms()
    assert set(rooms) == {"!a:hs", "!b:hs"}
    assert rooms["!a:hs"].display_name == "Planning"
    # Member names come through, so the UI can name the sender of a message.
    assert rooms["!a:hs"].user_name("@bob:hs") == "Bob"
    # An unknown member yields None, not a guess: the UI falls back to the id.
    assert rooms["!a:hs"].user_name("@dave:hs") is None


def test_a_room_the_snapshot_has_not_named_yet_is_still_listed(fake_core):
    """An event can beat the name of its room.

    Dropping it would lose a message; showing the raw id until the core catches
    up is the lesser problem, and it resolves on the next sync.
    """
    client = ShelltrixClient(creds=creds)
    room = client.transport.room("!late:hs")
    assert room.room_id == "!late:hs"
    assert room.display_name == "!late:hs"


def test_next_batch_is_absent_without_lying(fake_core):
    """The sidebar prints this token; inventing one would be worse than none."""
    client = ShelltrixClient(creds=creds)
    assert client.next_batch is None


@pytest.mark.asyncio
async def test_load_local_store_does_nothing_without_a_store(fake_core):
    """Nothing to load is not an error.

    The Rust core has no crypto store yet; `start()` warns about that, and this
    must not turn into a crash on the way past.
    """
    client = ShelltrixClient(creds=creds)
    client.load_local_store()


@pytest.mark.asyncio
async def test_stop_stops_the_core(fake_core):
    client = ShelltrixClient(creds=creds)
    client.start()
    await client.stop()
    assert fake_core.stops == 1


@pytest.mark.asyncio
async def test_stop_without_start_is_safe(fake_core):
    client = ShelltrixClient(creds=creds)
    await client.stop()
    assert fake_core.stops == 1


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
@dataclass
class Handlers:
    messages: list[tuple[Room, MessageEvent]] = field(default_factory=list)
    images: list[tuple[Room, ImageEvent]] = field(default_factory=list)
    reactions: list[tuple[Room, str, str, str]] = field(default_factory=list)
    typing: list[tuple[str, list[str]]] = field(default_factory=list)
    invites: list[tuple[str, Room, str]] = field(default_factory=list)
    first_syncs: int = 0

    def attach(self, client: ShelltrixClient) -> "Handlers":
        async def on_message(room, event):
            self.messages.append((room, event))

        async def on_image(room, event):
            self.images.append((room, event))

        async def on_reaction(room, target, key, sender):
            self.reactions.append((room, target, key, sender))

        async def on_typing(room_id, users):
            self.typing.append((room_id, users))

        async def on_invite(room_id, room, inviter):
            self.invites.append((room_id, room, inviter))

        async def on_first_sync():
            self.first_syncs += 1

        client.on_message = on_message
        client.on_image = on_image
        client.on_reaction = on_reaction
        client.on_typing = on_typing
        client.on_invite = on_invite
        client.on_first_sync = on_first_sync
        return self


@pytest.mark.asyncio
async def test_a_message_reaches_the_message_handler(fake_core):
    fake_core.rooms = [RoomSnapshot(room_id="!r:hs", display_name="Planning")]
    client = ShelltrixClient(creds=creds)
    seen = Handlers().attach(client)
    await client.transport.dispatch(message("hello"))

    assert len(seen.messages) == 1
    room, event = seen.messages[0]
    assert room.display_name == "Planning"
    assert event.body == "hello"
    assert isinstance(event, MessageEvent)


@pytest.mark.asyncio
async def test_an_image_reaches_the_image_handler(fake_core):
    client = ShelltrixClient(creds=creds)
    seen = Handlers().attach(client)
    await client.transport.dispatch(
        StreamEvent(
            kind=KIND_IMAGE,
            room_id="!r:hs",
            sender="@bob:hs",
            event_id="$4",
            body="cat.png",
            url="mxc://hs/abc",
        )
    )
    assert len(seen.images) == 1
    assert seen.images[0][1].url == "mxc://hs/abc"
    assert seen.messages == [], "an image must not also be announced as a message"


@pytest.mark.asyncio
async def test_a_reaction_reaches_the_reaction_handler_only(fake_core):
    """A reaction is an `m.room.message` with an empty body.

    Sent to `on_message` as well, it renders as an empty bubble — which is
    exactly what the classifier exists to prevent.
    """
    client = ShelltrixClient(creds=creds)
    seen = Handlers().attach(client)
    await client.transport.dispatch(
        StreamEvent(
            kind=KIND_REACTION,
            room_id="!r:hs",
            sender="@carol:hs",
            event_id="$5",
            target="$1",
            key="\N{THUMBS UP SIGN}",
        )
    )
    assert seen.reactions == [(seen.reactions[0][0], "$1", "\N{THUMBS UP SIGN}", "@carol:hs")]
    assert seen.messages == []


@pytest.mark.asyncio
async def test_typing_carries_who_is_typing(fake_core):
    client = ShelltrixClient(creds=creds)
    seen = Handlers().attach(client)
    await client.transport.dispatch(
        StreamEvent(kind=KIND_TYPING, room_id="!r:hs", users=("@bob:hs", "@carol:hs"))
    )
    # A tuple from Rust becomes a list, because the UI's handler signature says so.
    assert seen.typing == [("!r:hs", ["@bob:hs", "@carol:hs"])]


@pytest.mark.asyncio
async def test_typing_that_stopped_is_announced_with_nobody(fake_core):
    """An empty list clears the indicator.

    Dropping the event instead would leave it lit forever.
    """
    client = ShelltrixClient(creds=creds)
    seen = Handlers().attach(client)
    await client.transport.dispatch(StreamEvent(kind=KIND_TYPING, room_id="!r:hs"))
    assert seen.typing == [("!r:hs", [])]


@pytest.mark.asyncio
async def test_an_invite_names_who_asked(fake_core):
    client = ShelltrixClient(creds=creds)
    seen = Handlers().attach(client)
    await client.transport.dispatch(
        StreamEvent(kind=KIND_INVITE, room_id="!r:hs", sender="@bob:hs")
    )
    assert len(seen.invites) == 1
    assert seen.invites[0][0] == "!r:hs"
    assert seen.invites[0][2] == "@bob:hs"


@pytest.mark.asyncio
async def test_an_event_with_no_handler_is_dropped_quietly(fake_core):
    """A user who registered no handler must not crash the sync loop."""
    client = ShelltrixClient(creds=creds)
    await client.transport.dispatch(message("hello"))


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
async def wait_for(predicate, *, timeout: float = 5.0) -> bool:
    """Waits for `predicate`, in real time, and reports whether it came true.

    Real time rather than `asyncio.sleep(0)`: the loop calls the core through
    `asyncio.to_thread`, and a spin on `sleep(0)` cannot carry a thread round
    trip — the loop would look frozen when it is merely waiting for a worker.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


@pytest.mark.asyncio
async def test_the_loop_delivers_and_then_fires_first_sync_once(fake_core):
    fake_core.events = [message("one"), message("two"), None, None]
    fake_core.rooms = [RoomSnapshot(room_id="!r:hs", display_name="Planning")]
    client = ShelltrixClient(creds=creds)
    seen = Handlers().attach(client)
    client.start()

    assert await wait_for(lambda: len(seen.messages) == 2 and fake_core.polls >= 4)
    await client.stop()

    assert [event.body for _, event in seen.messages] == ["one", "two"]
    # A quiet long poll must not re-announce the first sync: the UI would
    # repaint its whole room list every 30 seconds for nothing.
    assert seen.first_syncs == 1
    assert client.first_sync_done is True
    assert client.sync_state == "online"


@pytest.mark.asyncio
async def test_a_quiet_account_still_reports_its_first_sync(fake_core):
    """No events at all, only expired long polls.

    This is the case that justifies asking the core for the state rather than
    inferring it: inferring from "an event arrived" leaves the room list blank
    forever on a perfectly healthy account.
    """
    fake_core.events = [None, None, None]
    client = ShelltrixClient(creds=creds)
    seen = Handlers().attach(client)
    client.start()

    assert await wait_for(lambda: seen.first_syncs >= 1)
    await client.stop()

    assert seen.first_syncs == 1
    assert seen.messages == []


@pytest.mark.asyncio
async def test_the_loop_restarts_the_core_when_its_sync_ends(fake_core):
    """The core owns its own sync task, so recovery means making a new one.

    matrix-nio's loop recovers by calling `sync()` again; here the queue is
    closed and there is nothing left to poll, so a client that does not restart
    would go permanently quiet.
    """
    fake_core.events = [message("before"), None]
    client = ShelltrixClient(creds=creds)
    Handlers().attach(client)
    client.start()

    # One start is `start()`'s; the second is the restart after the queue drains.
    assert await wait_for(lambda: fake_core.starts >= 2), "the loop never rebuilt the core's sync"
    await client.stop()


@pytest.mark.asyncio
async def test_the_loop_backs_off_when_the_core_cannot_start(fake_core):
    """A homeserver that is down must not become a busy loop."""
    fake_core.events = []
    fake_core.start_failures = 10_000
    client = ShelltrixClient(creds=creds)
    Handlers().attach(client)
    client.start()

    # Give it room to try several times, then check it did not try thousands.
    await wait_for(lambda: fake_core.starts >= 2, timeout=1.0)
    await client.stop()

    assert client.sync_state in {"offline", "reconnecting", "syncing"}
    # The backoff is real: without it this would spin at thousands of attempts
    # a second and burn the CPU of a laptop on a plane.
    assert fake_core.starts <= 5, "no backoff between attempts"


@pytest.fixture
def fake_core(monkeypatch):
    """A `FakeCore` wired over the seam, with the Rust backend selected."""
    return FakeCore().install(monkeypatch)