"""Tests pour la reconnexion automatique (backoff exponentiel).

Vérifie le contrat du `_run_sync_forever()` de ShelltrixClient : une panne
réseau (sync() qui lève) ne doit pas faire tomber la tâche pour toujours
— elle passe en état "offline"/"reconnecting", attend, puis retente ; un
sync réussi repasse l'état à "online" et remet le backoff à zéro.
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
    """Après des échecs successifs, le sync finit par réussir : l'état
    repasse en "online" et la boucle continue."""
    calls = 0

    async def flaky_sync(**kwargs) -> object:
        nonlocal calls
        calls += 1
        # échec réseau les 2 premiers appels, puis succès
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
        # Laisser la boucle tourner assez longtemps pour passer 2 échecs
        # (backoff 1s + 2s) puis un succès.
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
    """Tant qu'aucun sync n'a réussi, un échec expose l'état 'offline'."""
    async def always_fail(**kwargs) -> object:
        raise ConnectionError("down")

    creds = Credentials("hs", "@u:hs", "dev", "token")
    with patch("shelltrix.matrix_client.AsyncClient") as mock_client_cls:
        client_inst = mock_client_cls.return_value
        client_inst.sync = AsyncMock(side_effect=always_fail)
        client_inst.add_event_callback = MagicMock()
        client_inst.add_to_device_callback = MagicMock()

        nc = ShelltrixClient(creds)
        nc._ever_connected = False  # jamais connecté

        # Premier appel direct : l'exception est avalée par la boucle.
        async def run() -> None:
            await nc._run_sync_forever()

        task = asyncio.create_task(run())
        # backoff 1s → l'état reste "offline" puis on annule
        await asyncio.sleep(1.2)
        state = nc.sync_state
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert state == "offline"


async def _wait_first_sync(nc: ShelltrixClient, timeout: float = 2.0) -> None:
    """Attend la fin du premier sync (la boucle, elle, tourne indéfiniment)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not nc.first_sync_done:
        assert asyncio.get_running_loop().time() < deadline, "premier sync non terminé"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_start_does_not_block_on_first_sync() -> None:
    """start() rend la main immédiatement : le premier sync part en tâche de
    fond (sinon l'UI reste invisible pendant les ~10 s du full_state sync)."""
    gate = asyncio.Event()

    async def slow_sync(**kwargs) -> object:
        await gate.wait()
        return object()

    creds = Credentials("hs", "@u:hs", "dev", "token")
    with patch("shelltrix.matrix_client.AsyncClient") as mock_client_cls:
        client_inst = mock_client_cls.return_value
        client_inst.sync = AsyncMock(side_effect=slow_sync)
        client_inst.next_batch = None  # premier sync = full_state
        client_inst.add_event_callback = MagicMock()
        client_inst.add_to_device_callback = MagicMock()

        nc = ShelltrixClient(creds)
        nc.load_local_store = lambda: None  # pas de store E2EE sur le disque
        nc.start()
        # start() est synchrone et n'attend pas le sync : l'état "syncing"
        # est visible tout de suite.
        assert nc.sync_state == "syncing"
        assert nc._sync_task is not None and not nc._sync_task.done()

        # Le sync ne démarre qu'une fois la boucle d'événements rendue (donc
        # après le retour de start()) et il était encore en attente ici.
        await asyncio.sleep(0)
        assert client_inst.sync.await_count == 1
        # Sans next_batch en cache, la première passe est un full_state sync.
        assert client_inst.sync.await_args.kwargs["full_state"] is True

        gate.set()
        await _wait_first_sync(nc)
        assert nc.sync_state == "online"
        nc._sync_task.cancel()


@pytest.mark.asyncio
async def test_first_sync_callback_fires_once() -> None:
    """Le callback UI est appelé après le premier sync réussi, une seule fois,
    même si les syncs suivants.aboutissent aussi."""
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
        await asyncio.sleep(0.2)  # plusieurs itérations de la boucle
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
    """Un callback UI qui lève ne doit pas être pris pour une panne réseau :
    la boucle de sync continue (état "online", pas de backoff "offline")."""
    async def ok_sync(**kwargs) -> object:
        return object()

    async def boom() -> None:
        raise RuntimeError("bug de rendu")

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
    """App vide : héberge la ChatScreen pour un test de montage."""


@pytest.mark.asyncio
async def test_chat_screen_populates_rooms_on_first_sync() -> None:
    """La ChatScreen s'affiche sans attendre le sync (liste vide + état
    "syncing…"), puis la liste des salons se remplit au premier sync."""
    from shelltrix.screens.chat import ChatScreen

    creds = Credentials("hs", "@u:hs", "dev", "token")
    with patch("shelltrix.matrix_client.AsyncClient") as mock_client_cls:
        client_inst = mock_client_cls.return_value
        gate = asyncio.Event()

        async def gated_sync(**kwargs: object) -> object:
            await gate.wait()  # le sync ne répond pas tout de suite
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
            # Monté et peint : le sync est en cours, la liste est vide.
            assert nc.sync_state == "syncing"
            assert len(nc.rooms()) == 0
            assert nc.first_sync_done is False

            gate.set()
            # Le sync répond : la liste se remplit sans réécrire la méthode.
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
