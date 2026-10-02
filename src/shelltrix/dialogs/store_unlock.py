"""E2EE store restoration at startup when the keyring key is missing.

`StoreUnlockDialog` modal extracted from `app.py`. Deferred imports of
ChatScreen/LoginScreen while they live in app.py (steps 11 and 12),
cf. NOTES.md.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

# ChatScreen/LoginScreen are extracted in screens/ (steps 11-12); they are
# imported at the top of the module. Remaining notes: cf. NOTES.md.
from ..config import (
    Credentials,
    StoreLockedError,
    decrypt_store,
    remove_store,
)
from ..matrix_client import ShelltrixClient
from ..screens.chat import ChatScreen
from ..screens.login import LoginScreen


class StoreUnlockDialog(ModalScreen[None]):
    """Asks for the recovery key to decrypt the E2EE store at startup,
    when the keyring key is absent (restoration)."""

    BINDINGS = [
        ("escape", "cancel", "Sign out"),
    ]

    def __init__(self, creds: Credentials, client: ShelltrixClient) -> None:
        super().__init__()
        self.creds = creds
        self.client = client

    def compose(self) -> ComposeResult:
        with Vertical(id="unlock-dialog"):
            yield Static("Recovery key required", id="unlock-title")
            yield Static(
                "Your E2EE session is encrypted at rest and its store key is "
                "missing from the system keyring. Enter the recovery key you "
                "saved with /recovery to restore this session.",
                id="unlock-hint",
            )
            yield Input(placeholder="Recovery key", id="unlock-key")
            yield Label("", id="unlock-status")
            with Horizontal(id="unlock-actions"):
                yield Button("Restore", id="unlock-restore", variant="primary", classes="-primary")
                yield Button("Sign out & start fresh", id="unlock-logout", classes="-danger")

    def on_mount(self) -> None:
        self.query_one("#unlock-key", Input).focus()

    def _status(self, message: str, *, kind: str = "") -> None:
        status = self.query_one("#unlock-status", Label)
        status.set_classes(kind)
        status.update(message)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "unlock-restore":
            await self._restore()
        elif event.button.id == "unlock-logout":
            await self._cancel()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "unlock-key":
            await self._restore()

    async def _restore(self) -> None:
        raw = self.query_one("#unlock-key", Input).value
        if not raw.strip():
            self._status("Type your recovery key.", kind="error")
            return
        try:
            decrypt_store(recovery_key=raw)
            # Store decrypted: now load the local E2EE keys.
            self.client.load_local_store()
        except StoreLockedError as exc:
            self._status(str(exc), kind="error")
            return
        self.dismiss()
        self.app.call_after_refresh(self._go_chat)

    async def _go_chat(self) -> None:
        await self.app.push_screen(ChatScreen(self.client))

    async def _cancel(self) -> None:
        self.creds.remove()
        remove_store()
        self.dismiss()
        self.app.call_after_refresh(self._go_login)

    async def _go_login(self) -> None:
        await self.app.switch_screen(LoginScreen())

    async def action_cancel(self) -> None:
        await self._cancel()