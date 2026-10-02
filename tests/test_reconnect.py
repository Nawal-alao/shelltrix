"""Tests for automatic reconnection (exponential backoff).

Verifies the contract of ShelltrixClient's `_run_sync_forever()`: a network
outage (a sync() that raises) must not bring the task down for good — it
switches to the "offline"/"reconnecting" state, waits, then retries; a
successful sync flips the state back to "online" and resets the backoff to
zero.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from textual.app import App

from shelltrix.config import Credentials
from shelltrix.matrix_client import ShelltrixClient


@pytest.mark.asyncio
async def test_retries_and_recovers_after_failure() -> None:
    """After successive failures, the sync eventually succeeds: the state
    goes back to "online" and the loop continues."""
    calls = 0

    async def flaky_sync(**kwargs) -> object:
        nonlocal calls
        calls += 1
        # network outage on the first 2 calls, then success
        if calls <= 2:
            raise ConnectionError("connection reset")
        return object()

    creds = Credentials("hs", "@u:hs", "dev", "token")
    with patch("shelltrix.matrix_client.AsyncClient") as mock_client_cls:
        client_inst = mock_client_cls.return_value
        client_inst.sync = AsyncMock(side_effect=flaky_sync)
        client_inst.next_batch = "abc"
        client_inst.add_event_callback = MagicMock()
        client_inst.add_to_device_callback = MagicMock()
        client_inst.rooms = {}

        nc = ShelltrixClient(creds)
        nc._ever_connected = True

        async def run() -> None:
            await nc._run_sync_forever()

        task = asyncio.create_task(run())
        # Let the loop run long enough to get past 2 failures
        # (backoff 1s + 2s) and then a success.
        await asyncio.sleep(3.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert nc.sync_state == "online"
        assert calls >= 3


@pytest.mark.asyncio
async def test_offline_when_never_connected() -> None:
    """As long as no sync has succeeded, a failure exposes the 'offline' state."""
    async def always_fail(**kwargs) -> object:
        raise ConnectionError("down")

    creds = Credentials("hs", "@u:hs", "dev", "token")
    with patch("shelltrix.matrix_client.AsyncClient") as mock_client_cls:
        client_inst = mock_client_cls.return_value
        client_inst.sync = AsyncMock(side_effect=always_fail)
        client_inst.add_event_callback = MagicMock()
        client_inst.add_to_device_callback = MagicMock()

        nc = ShelltrixClient(creds)
        nc._ever_connected = False  # never connected

        # First direct call: the exception is swallowed by the loop.
        async def run() -> None:
            await nc._run_sync_forever()

        task = asyncio.create_task(run())
        # backoff 1s → the state stays "offline", then we cancel
        await asyncio.sleep(1.2)
        state = nc.sync_state
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert state == "offline"


async def _wait_first_sync(nc: ShelltrixClient, timeout: float = 2.0) -> None:
    """Waits for the first sync to finish (the loop itself runs indefinitely)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not nc.first_sync_done:
        assert asyncio.get_running_loop().time() < deadline, "first sync not finished"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_start_does_not_block_on_first_sync() -> None:
    """start() returns immediately: the first sync runs as a background task
    (otherwise the UI stays invisible for the ~10 s of the full_state sync)."""
    gate = asyncio.Event()

    async def slow_sync(**kwargs) -> object:
        await gate.wait()
        return object()

    creds = Credentials("hs", "@u:hs", "dev", "token")
    with patch("shelltrix.matrix_client.AsyncClient") as mock_client_cls:
        client_inst = mock_client_cls.return_value
        client_inst.sync = AsyncMock(side_effect=slow_sync)
        client_inst.next_batch = None  # first sync = full_state
        client_inst.add_event_callback = MagicMock()
        client_inst.add_to_device_callback = MagicMock()

        nc = ShelltrixClient(creds)
        nc.load_local_store = lambda: None  # no E2EE store on disk
        nc.start()
        # start() is synchronous and does not wait for the sync: the "syncing"
        # state is visible right away.
        assert nc.sync_state == "syncing"
        assert nc._sync_task is not None and not nc._sync_task.done()

        # The sync only starts once the event loop has been handed back
        # (i.e. after start() returns), and it was still waiting here.
        await asyncio.sleep(0)
        assert client_inst.sync.await_count == 1
        # With no next_batch in cache, the first pass is a full_state sync.
        assert client_inst.sync.await_args.kwargs["full_state"] is True

        gate.set()
        await _wait_first_sync(nc)
        assert nc.sync_state == "online"
        nc._sync_task.cancel()


@pytest.mark.asyncio
async def test_first_sync_callback_fires_once() -> None:
    """The UI callback is called after the first successful sync, only once,
    even if the following syncs succeed too."""
    calls = 0

    async def counting_sync(**kwargs) -> object:
        nonlocal calls
        calls += 1
        return object()

    async def on_first_sync() -> None:
        fired.append(1)

    fired: list[int] = []
    creds = Credentials("hs", "@u:hs", "dev", "token")
    with patch("shelltrix.matrix_client.AsyncClient") as mock_client_cls:
        client_inst = mock_client_cls.return_value
        client_inst.sync = AsyncMock(side_effect=counting_sync)
        client_inst.next_batch = "tok"
        client_inst.add_event_callback = MagicMock()
        client_inst.add_to_device_callback = MagicMock()

        nc = ShelltrixClient(creds)
        nc.load_local_store = lambda: None
        nc.on_first_sync = on_first_sync

        task = asyncio.create_task(nc._run_sync_forever())
        await asyncio.sleep(0.2)  # several loop iterations
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(fired) == 1
        assert nc.first_sync_done is True
        assert nc.sync_state == "online"


@pytest.mark.asyncio
async def test_ui_error_in_first_sync_does_not_kill_sync_loop() -> None:
    """A UI callback that raises must not be mistaken for a network outage:
    the sync loop continues (state "online", no "offline" backoff)."""
    async def ok_sync(**kwargs) -> object:
        return object()

    async def boom() -> None:
        raise RuntimeError("render bug")

    creds = Credentials("hs", "@u:hs", "dev", "token")
    with patch("shelltrix.matrix_client.AsyncClient") as mock_client_cls:
        client_inst = mock_client_cls.return_value
        client_inst.sync = AsyncMock(side_effect=ok_sync)
        client_inst.next_batch = "tok"
        client_inst.add_event_callback = MagicMock()
        client_inst.add_to_device_callback = MagicMock()

        nc = ShelltrixClient(creds)
        nc.load_local_store = lambda: None
        nc.on_first_sync = boom

        task = asyncio.create_task(nc._run_sync_forever())
        await asyncio.sleep(0.2)
        state = nc.sync_state
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert state == "online"


class _HostApp(App):
    """Empty app: hosts the ChatScreen for a mounting test."""


@pytest.mark.asyncio
async def test_chat_screen_populates_rooms_on_first_sync() -> None:
    """The ChatScreen displays without waiting for the sync (empty list +
    "syncing…" state), then the room list fills in on the first sync."""
    from shelltrix.screens.chat import ChatScreen

    creds = Credentials("hs", "@u:hs", "dev", "token")
    with patch("shelltrix.matrix_client.AsyncClient") as mock_client_cls:
        client_inst = mock_client_cls.return_value
        gate = asyncio.Event()

        async def gated_sync(**kwargs: object) -> object:
            await gate.wait()  # the sync does not answer right away
            return object()

        client_inst.sync = AsyncMock(side_effect=gated_sync)
        client_inst.next_batch = None
        client_inst.add_event_callback = MagicMock()
        client_inst.add_to_device_callback = MagicMock()
        client_inst.close = AsyncMock()
        client_inst.user_id = "@u:hs"

        nc = ShelltrixClient(creds)
        nc.load_local_store = lambda: None
        nc.stop = AsyncMock()
        rooms: dict[str, object] = {}
        client_inst.rooms = rooms

        app = _HostApp()
        async with app.run_test() as pilot:
            await app.push_screen(ChatScreen(nc))
            await pilot.pause()
            # Mounted and painted: the sync is running, the list is empty.
            assert nc.sync_state == "syncing"
            assert len(nc.rooms()) == 0
            assert nc.first_sync_done is False

            gate.set()
            # The sync answers: the list fills in without rewriting the method.
            room = MagicMock()
            room.room_id = "!r:hs"
            room.display_name = "Matrix HQ"
            rooms["!r:hs"] = room
            await _wait_first_sync(nc)
            await pilot.pause()

            assert nc.first_sync_done is True
            assert nc.sync_state == "online"
            assert nc.rooms() != {}
            await nc.stop()
            app.pop_screen()
