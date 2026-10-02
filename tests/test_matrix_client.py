"""Integration tests for the ShelltrixClient layer (Matrix proto).

We mock `nio.AsyncClient` (no network connection) to verify the real
behaviours of the ShelltrixClient layer: send-time security policy, event
propagation (messages, invites, typing), and upload/send error handling.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shelltrix.config import Credentials
from shelltrix.matrix_client import ShelltrixClient


def make_client(**overrides: object) -> ShelltrixClient:
    """Builds a ShelltrixClient whose underlying AsyncClient is a mock."""
    creds = Credentials("hs", "@me:hs", "dev1", "token")
    with patch("shelltrix.matrix_client.AsyncClient") as cls:
        inst = cls.return_value
        inst.room_send = AsyncMock()
        inst.user_id = "@me:hs"
        inst.add_event_callback = MagicMock()
        inst.add_to_device_callback = MagicMock()
        inst.rooms = {}
        nc = ShelltrixClient(creds)
    for k, v in overrides.items():
        setattr(nc.client, k, v)
    return nc


@pytest.mark.asyncio
async def test_send_message_room_send() -> None:
    nc = make_client()
    await nc.send_message("!r:hs", "hello")
    nc.client.room_send.assert_awaited_once_with(
        room_id="!r:hs",
        message_type="m.room.message",
        content={"msgtype": "m.text", "body": "hello"},
    )


@pytest.mark.asyncio
async def test_send_security_policy_blocked_devices() -> None:
    """In an encrypted room with unverified devices, sending is blocked
    (LocalProtocolError) and reported to the UI — we never transmit to a
    potentially compromised recipient."""
    from nio.exceptions import LocalProtocolError

    nc = make_client()
    nc.client.room_send = AsyncMock(
        side_effect=LocalProtocolError("device not verified")
    )
    reported: list[tuple[str, str]] = []

    async def on_send_error(rid: str, msg: str) -> None:
        reported.append((rid, msg))

    nc.on_send_error = on_send_error  # type: ignore[assignment]
    await nc.send_message("!r:hs", "secret")
    assert reported, "the security failure must be reported to the UI"
    assert reported[0][0] == "!r:hs"


@pytest.mark.asyncio
async def test_send_network_error_reported() -> None:
    nc = make_client()
    nc.client.room_send = AsyncMock(side_effect=ConnectionError("offline"))
    reported: list[tuple[str, str]] = []

    async def on_send_error(rid: str, msg: str) -> None:
        reported.append((rid, msg))

    nc.on_send_error = on_send_error  # type: ignore[assignment]
    await nc.send_message("!r:hs", "hi")  # type: ignore[arg-type]
    assert reported, "a network error must be reported to the UI"
    assert "ConnectionError" in reported[0][1]


@pytest.mark.asyncio
async def test_handle_invite_for_own_user_only() -> None:
    nc = make_client()
    fired: list[tuple[str, object, str]] = []

    async def on_invite(rid: str, room: object, inviter: str) -> None:
        fired.append((rid, room, inviter))

    nc.on_invite = on_invite  # type: ignore[assignment]

    room = MagicMock()
    # Not for us: different state_key → ignored
    event = MagicMock(state_key="@other:hs", sender="@inviter:hs")
    # Stand in for _handle_invite to test its body directly via the callback
    await nc._handle_invite(room, event)
    assert nc.client.user_id == "@me:hs"
    # _handle_invite checks state_key == user_id; state_key != me → nothing
    assert fired == []


@pytest.mark.asyncio
async def test_handle_invite_forwarded_for_own_user() -> None:
    nc = make_client()
    fired: list[tuple[str, object, str]] = []

    async def on_invite(rid: str, room: object, inviter: str) -> None:
        fired.append((rid, room, inviter))

    nc.on_invite = on_invite  # type: ignore[assignment]

    room = MagicMock(room_id="!inv:hs")
    event = MagicMock(state_key="@me:hs", sender="@inviter:hs")
    await nc._handle_invite(room, event)
    assert fired == [("!inv:hs", room, "@inviter:hs")]


@pytest.mark.asyncio
async def test_handle_typing_forwards() -> None:
    nc = make_client()
    seen: list[tuple[str, list[str]]] = []

    async def on_typing(rid: str, users: list[str]) -> None:
        seen.append((rid, users))

    nc.on_typing = on_typing  # type: ignore[assignment]
    room = MagicMock(room_id="!r:hs")
    event = MagicMock(users=["@a:hs", "@b:hs"])
    await nc._handle_typing(room, event)
    assert seen == [("!r:hs", ["@a:hs", "@b:hs"])]


@pytest.mark.asyncio
async def test_handle_message_forwards() -> None:
    nc = make_client()
    seen = []

    async def on_message(room, event) -> None:
        seen.append((room, event))

    nc.on_message = on_message  # type: ignore[assignment]
    room = MagicMock()
    event = MagicMock()
    await nc._handle_message(room, event)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_part_room_sends_farewell_then_leaves() -> None:
    nc = make_client()
    nc.client.room_leave = AsyncMock()
    await nc.part_room("!r:hs", "bye")
    assert nc.client.room_send.await_count == 1
    nc.client.room_leave.assert_awaited_once_with("!r:hs")


@pytest.mark.asyncio
async def test_send_image_missing_file() -> None:
    nc = make_client()
    reported = []

    async def on_send_error(rid: str, msg: str) -> None:
        reported.append((rid, msg))

    nc.on_send_error = on_send_error  # type: ignore[assignment]
    await nc.send_image("!r:hs", "/nonexistent/this/file.png")  # type: ignore[arg-type]
    assert reported
    assert "File not found" in reported[0][1]


@pytest.mark.asyncio
async def test_room_messages_success_returns_response() -> None:
    """room_messages() returns the nio response on success (scrollback)."""
    nc = make_client()
    resp = object()
    nc.client.room_messages = AsyncMock(return_value=resp)
    # Import the expected type: we simulate a RoomMessagesResponse for the
    # isinstance in ShelltrixClient.room_messages.
    from nio import RoomMessagesResponse

    resp = MagicMock(spec=RoomMessagesResponse)
    resp.chunk = []
    nc.client.room_messages = AsyncMock(return_value=resp)
    out = await nc.room_messages("!r:hs", start="T1", limit=40)
    assert out is resp
    nc.client.room_messages.assert_awaited_once_with("!r:hs", start="T1", limit=40)


@pytest.mark.asyncio
async def test_room_messages_error_returns_none() -> None:
    """room_messages() returns None if the response is not positive."""
    nc = make_client()
    nc.client.room_messages = AsyncMock(return_value=MagicMock())  # not a RoomMessagesResponse
    out = await nc.room_messages("!r:hs", limit=40)
    assert out is None


@pytest.mark.asyncio
async def test_room_messages_exception_returns_none() -> None:
    """room_messages() falls back to None if the call raises (network)."""
    nc = make_client()
    nc.client.room_messages = AsyncMock(side_effect=RuntimeError("offline"))
    out = await nc.room_messages("!r:hs")
    assert out is None
