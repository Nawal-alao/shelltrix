"""Textual interface of shelltrix.

This module assembles the application: `ShelltrixApp` (main Textual app) and
the entry point `run()`. Screens, dialogs and helpers have been extracted
into separate modules (screens/, dialogs/, formatting.py, sidebar.py,
widgets.py) — see NOTES.md for the detail of the split.
"""

from __future__ import annotations

import argparse
from typing import Sequence

from textual.app import App

from . import __version__, themes
from .accounts import get_manager
from .config import (
    Credentials,
    StoreLockedError,
    first_run_done,
    mark_first_run_done,
    skip_splash,
)
from .dialogs.command_palette import CommandPalette
from .dialogs.store_unlock import StoreUnlockDialog
from .matrix_client import ShelltrixClient
from .screens.account_picker import AccountPickerScreen
from .screens.chat import ChatScreen
from .screens.login import LoginScreen
from .screens.splash import SplashScreen

# The markup colors (ACCENT/DANGER) follow the active theme: they are
# re-evaluated on every theme switch by _apply_theme_globals().
# After the module split, consumers read themes.accent() and
# themes.danger() directly (cf. NOTES.md). The globals stay here,
# maintained by ShelltrixApp but no longer used.
ACCENT = "a2d399"
DANGER = "ffb4ab"


def _apply_theme_globals() -> None:
    """Repoints the markup constants (ACCENT/DANGER) to the active theme."""
    global ACCENT, DANGER
    ACCENT = themes.accent()
    DANGER = themes.danger()


_apply_theme_globals()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class ShelltrixApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "shelltrix"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [("ctrl+p", "command_palette", "Commands")]

    def __init__(self) -> None:
        super().__init__()
        # Themes are registered BEFORE the first mount: the CSS
        # variables ($bg, $primary, ...) must exist when app.tcss is compiled.
        themes.activate(self)
        _apply_theme_globals()

    def cycle_theme(self) -> str:
        """Switches to the next theme and persists the choice."""
        name = themes.cycle(self.theme)
        self.theme = name
        themes.tick(name)
        themes.save_pref(name)
        _apply_theme_globals()
        self.notify(f"Theme: [bold]{themes.label(name)}[/bold]", title="Theme")
        return name

    async def on_mount(self) -> None:
        # The welcome splash is reserved for the very first use:
        # `skip_splash` stays the manual opt-out (scripted launch), the
        # first-run marker does the rest. We mark it *before* pushing the
        # screen: once the splash is displayed, it never comes back, even
        # if the user quits the app during the animation.
        if skip_splash() or first_run_done():
            await self.begin()
            return
        mark_first_run_done()
        await self.push_screen(SplashScreen())

    async def begin(self) -> None:
        """After the splash: login or chat resumption depending on the creds."""
        manager = get_manager()
        # If several accounts are stored, offer the picker
        if manager.count() > 1:
            await self.push_screen(AccountPickerScreen())
            return
        creds = Credentials.load()
        if creds is None:
            await self.push_screen(LoginScreen())
        else:
            await self.start_chat(creds)

    def action_command_palette(self) -> None:
        self.push_screen(CommandPalette())

    # Textual's native theme toggle shortcuts cycle our themes.
    def action_change_theme(self) -> None:
        self.cycle_theme()

    def action_search_themes(self) -> None:
        self.cycle_theme()

    async def start_chat(self, creds: Credentials) -> None:
        client = ShelltrixClient(creds=creds)
        try:
            client.load_local_store()
        except StoreLockedError:
            await self.push_screen(StoreUnlockDialog(creds, client))
            return
        await self.push_screen(ChatScreen(client))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shelltrix",
        description="shelltrix — a premium Matrix TUI client, in Python "
        "(matrix-nio + Textual).",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"shelltrix {__version__}",
        help="show the version and exit",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> None:
    # `--version`/`-V`/`--help` are handled by argparse (action="version"
    # prints and exits, without launching the Textual app).
    _build_parser().parse_args(argv)
    ShelltrixApp().run()


if __name__ == "__main__":
    run()