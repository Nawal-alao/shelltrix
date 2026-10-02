"""Command palette (ctrl+p) — ultra-flat collage, opencode style.

Holds the `COMMANDS` registry, the section order and the
`CommandPalette` modal screen. Extracted from `app.py`.
"""

from __future__ import annotations

import asyncio
from typing import NamedTuple

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from .. import themes
from ..screens.chat import ChatScreen
from .join_room import JoinRoomDialog
from .recovery import RecoveryDialog
from .search import SearchDialog


class CommandEntry(NamedTuple):
    id: str
    title: str
    description: str
    key: str
    category: str
    icon: str = ""
    suggested: bool = False


# Icons per category (unicode, no NerdFont required)
_CATEGORY_ICONS = {
    "Navigation": "⌘",
    "Chat": "✎",
    "Action": "➤",
    "System": "⚙",
}

COMMANDS: list[CommandEntry] = [
    CommandEntry(
        "focus_rooms",
        "Focus rooms",
        "Jump to the rooms list",
        "ctrl+r",
        "Navigation",
        icon="⌘",
        suggested=True,
    ),
    CommandEntry(
        "focus_input",
        "Write a message",
        "Focus the message input",
        "ctrl+l",
        "Navigation",
        icon="⌘",
        suggested=True,
    ),
    CommandEntry(
        "toggle_sidebar",
        "Toggle sidebar",
        "Show or hide the right context panel",
        "ctrl+d",
        "Navigation",
        icon="⌘",
    ),
    CommandEntry(
        "clear_screen",
        "Clear screen",
        "Clear the active room timeline",
        "ctrl+k",
        "Chat",
        icon="✎",
    ),
    CommandEntry(
        "mark_read",
        "Mark as read",
        "Reset the unread counter",
        "",
        "Chat",
        icon="✎",
    ),
    CommandEntry(
        "search",
        "Search messages",
        "Find a message in your local history",
        "ctrl+f",
        "Chat",
        icon="✎",
        suggested=True,
    ),
    CommandEntry(
        "join_room",
        "Join a room",
        "Type an alias (#room:server)",
        "",
        "Chat",
        icon="✎",
    ),
    CommandEntry(
        "sendimg",
        "Send an image",
        "Insert /sendimg in the composer",
        "",
        "Action",
        icon="➤",
    ),
    CommandEntry(
        "open_last_link",
        "Open last link",
        "Open the recent URL of the active room",
        "",
        "Action",
        icon="➤",
    ),
    CommandEntry(
        "sync_status",
        "Sync status",
        "Show the current sync state",
        "",
        "System",
        icon="⚙",
    ),
    CommandEntry(
        "theme",
        "Switch theme",
        "Toggle between OpenCode Zen and Matrix Green",
        "",
        "System",
        icon="⚙",
    ),
    CommandEntry(
        "recovery",
        "Recovery key",
        "Show or regenerate the E2EE session recovery key",
        "",
        "System",
        icon="⚙",
    ),
    CommandEntry(
        "switch_account",
        "Switch account",
        "Return to account selection",
        "",
        "System",
        icon="⚙",
    ),
    CommandEntry(
        "logout",
        "Sign out",
        "Close the session and erase local data",
        "",
        "System",
        icon="⚙",
    ),
    CommandEntry(
        "quit",
        "Quit shelltrix",
        "Close the application",
        "ctrl+q",
        "System",
        icon="⏻",
    ),
]


# Section order (non-selectable headers) in the grouped view.
SECTIONS = ("Suggested", "Navigation", "Chat", "Action", "System")


class CommandPalette(ModalScreen[None]):
    """opencode-style command palette (ctrl+p): ultra-flat collage."""

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("up", "prev", "Previous"),
        ("down,ctrl+n", "next", "Next"),
        ("ctrl+p", "prev", "Previous"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="cp-dialog"):
            with Horizontal(id="cp-header"):
                yield Label("Commands", id="cp-header-title")
                yield Label("esc", id="cp-header-esc")
            yield Input(placeholder="Search", id="cp-input")
            yield ListView(id="cp-list")
            yield Static("No command found", id="cp-empty")

    def on_click(self, event: events.Click) -> None:
        if getattr(event.target, "id", None) == "cp-header-esc":
            self.dismiss()

    def on_resize(self) -> None:
        self._fit_list()

    async def on_mount(self) -> None:
        self.query_one("#cp-input", Input).focus()
        self._rows: list[CommandEntry | str] = []
        self._pop_lock = asyncio.Lock()
        await self._populate()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "cp-input":
            await self._populate()

    def _query(self) -> str:
        return self.query_one("#cp-input", Input).value.strip().lower()

    @staticmethod
    def _markup(entry: CommandEntry, cursor: bool) -> tuple[Text, Text]:
        """Builds a row's markup: (title+desc, shortcut)."""
        accent_text = themes.accent_text()
        muted = themes.muted()

        # Title + description
        if cursor:
            title = Text(entry.title, style=f"bold {accent_text}")
        else:
            title = Text(entry.title, style="bold")
        if not entry.key and entry.description:
            desc = f"  {entry.description}"
            title.append(desc, style=f"{accent_text}" if cursor else muted)

        # Clean keyboard shortcut (no brackets, right-aligned)
        right = Text()
        if entry.key:
            if cursor:
                right.append(entry.key, style=f"bold {accent_text}")
            else:
                right.append(entry.key, style=muted)

        return title, right

    @staticmethod
    def _build_row(entry: CommandEntry) -> ListItem:
        title, right = CommandPalette._markup(entry, False)
        return ListItem(
            Horizontal(
                Label(title, classes="cp-row-title"),
                Static(right, classes="cp-row-right"),
                classes="cp-row",
            )
        )

    @staticmethod
    def _build_header(section: str) -> ListItem:
        return ListItem(
            Label(section, classes="cp-section"),
            classes="cp-header-item",
        )

    def _matching(self, q: str) -> list[CommandEntry]:
        return [
            c
            for c in COMMANDS
            if (not q or q in c.title.lower() or q in c.description.lower())
        ]

    def _grouped(self, commands: list[CommandEntry]) -> list[CommandEntry | str]:
        rows: list[CommandEntry | str] = []
        for section in SECTIONS:
            members = [
                c
                for c in commands
                if (section == "Suggested" and c.suggested)
                or (section == c.category and not c.suggested)
            ]
            members.sort(key=lambda c: c.title)
            if members:
                rows.append(section)  # section header
                rows.extend(members)
        remaining = [c for c in commands if c.category not in SECTIONS]
        rows.extend(sorted(remaining, key=lambda c: (c.category, c.title)))
        return rows

    def _first_command_index(self) -> int:
        for i, entry in enumerate(self._rows):
            if isinstance(entry, CommandEntry):
                return i
        return 0

    def _fit_list(self) -> None:
        lv = self.query_one("#cp-list", ListView)
        if lv.styles.display == "none" or not lv.children:
            return
        total_rows = sum(
            2 if (isinstance(r, str) and i > 0) else 1
            for i, r in enumerate(self._rows)
        )
        max_rows = max(3, int(self.size.height * 0.75) - 6)
        lv.styles.height = min(total_rows, max_rows)

    def _refresh_row_markup(self) -> None:
        lv = self.query_one("#cp-list", ListView)
        if not self._rows or not lv.children:
            return
        idx = lv.index if lv.index is not None else -1
        for i, (child, entry) in enumerate(zip(lv.children, self._rows)):
            if not isinstance(entry, CommandEntry):
                continue
            try:
                title, right = self._markup(entry, i == idx)
                child.query_one(".cp-row-title", Label).update(title)
                child.query_one(".cp-row-right", Static).update(right)
            except Exception:
                pass

    async def _populate(self) -> None:
        # Fast typing spawns concurrent _populate calls (Input.Changed during
        # a clear/append). A lock serialises them: _rows and the list
        # children stay consistent, otherwise _move could index out of
        # bounds with partially replaced content.
        async with self._pop_lock:
            q = self._query()
            if q:
                self._rows = list(self._matching(q))
            else:
                self._rows = self._grouped(self._matching(""))
            lv = self.query_one("#cp-list", ListView)
            empty = self.query_one("#cp-empty", Static)
            if not any(self._rows):
                empty.update(
                    f"No command found for “{q}”" if q else "No command found"
                )
                await lv.clear()
                empty.styles.display = "block"
                lv.styles.display = "none"
                return
            empty.styles.display = "none"
            lv.styles.display = "block"
            await lv.clear()
            for entry in self._rows:
                if isinstance(entry, str):
                    await lv.append(self._build_header(entry))
                else:
                    item = self._build_row(entry)
                    item.data_id = entry.id  # type: ignore[attr-defined]
                    await lv.append(item)
            lv.index = self._first_command_index()
            self._fit_list()
            self._refresh_row_markup()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "cp-list":
            return
        self._refresh_row_markup()

    def _selected_id(self) -> str | None:
        lv = self.query_one("#cp-list", ListView)
        child = lv.highlighted_child
        return getattr(child, "data_id", None) if child is not None else None

    def _move(self, direction: int) -> None:
        lv = self.query_one("#cp-list", ListView)
        n = len(lv.children)
        if n == 0:
            return
        idx = 0 if lv.index is None else lv.index
        step = 0
        while step < n:
            idx = (idx + direction) % n
            step += 1
            if isinstance(self._rows[idx], CommandEntry):
                break
        lv.index = idx

    def action_next(self) -> None:
        self._move(1)

    def action_prev(self) -> None:
        self._move(-1)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        cmd_id = getattr(event.item, "data_id", None)
        if cmd_id is not None:
            await self._run(cmd_id)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "cp-input":
            cmd_id = self._selected_id()
            if cmd_id is not None:
                await self._run(cmd_id)

    async def _run(self, cmd_id: str) -> None:
        self.dismiss()

        async def _go() -> None:
            app = self.app
            if cmd_id == "quit":
                app.exit()
                return
            if cmd_id == "theme":
                app.cycle_theme()  # type: ignore[attr-defined]
                return
            chat = next(
                (
                    s
                    for s in reversed(app.screen_stack)
                    if isinstance(s, ChatScreen)
                ),
                None,
            )
            if chat is None:
                app.notify("This command is available in the chat")
                return
            if cmd_id == "focus_rooms":
                chat.action_focus_rooms()
            elif cmd_id == "focus_input":
                chat.action_focus_input()
            elif cmd_id == "toggle_sidebar":
                chat.action_toggle_sidebar()
            elif cmd_id == "clear_screen":
                chat.action_clear_screen()
            elif cmd_id == "mark_read":
                chat.action_mark_read()
            elif cmd_id == "open_last_link":
                chat.action_open_last_link()
            elif cmd_id == "sync_status":
                chat.action_sync_status()
            elif cmd_id == "sendimg":
                chat.action_insert_slash("/sendimg ")
            elif cmd_id == "join_room":
                await app.push_screen(JoinRoomDialog(chat))
            elif cmd_id == "search":
                await app.push_screen(SearchDialog(chat))
            elif cmd_id == "recovery":
                await app.push_screen(RecoveryDialog(chat))
            elif cmd_id == "switch_account":
                from ..screens.account_picker import AccountPickerScreen

                asyncio.create_task(
                    self._switch_account(app, chat)
                )
            elif cmd_id == "logout":
                # Switching screens inside an awaited callback deadlocks
                # (waiting for the current screen to close from its own
                # pump). So we detach it into a separate task.
                asyncio.create_task(chat.logout_and_return_to_login())

        # Closing the modal is asynchronous: we defer execution so the focus
        # lands on the chat screen once it is removed.
        # The screen's call queue awaits the callback anyway.
        self.app.call_after_refresh(_go)

    async def _switch_account(self, app, chat) -> None:
        """Returns to the account selection screen."""
        from ..screens.account_picker import AccountPickerScreen

        # Sign out the current account
        await chat.client.logout()
        await app.switch_screen(AccountPickerScreen())