"""Tests for the async contract of the ChatScreen handlers.

Every handler assigned to self.client.on_* must be a coroutine: ShelltrixClient
awaits them (e.g. matrix_client._handle_typing does `await self.on_typing(...)`),
so a synchronous function assigned in its place would blow up with a TypeError
on every event.
"""

from __future__ import annotations

import inspect

from shelltrix.screens.chat import ChatScreen


def test_typing_handler_is_coroutine() -> None:
    """_handle_typing must be async to satisfy TypingHandler."""
    assert inspect.iscoroutinefunction(ChatScreen._handle_typing)


def test_all_client_handlers_are_coroutines() -> None:
    """Every handler wired to self.client.on_* in on_mount must be async,
    otherwise the await inside ShelltrixClient fails at runtime."""
    for name in (
        "_handle_incoming_message",
        "_handle_incoming_image",
        "_handle_typing",
        "_show_invite_dialog",
        "_show_sas_dialog",
        "_on_send_error",
    ):
        assert inspect.iscoroutinefunction(getattr(ChatScreen, name)), name