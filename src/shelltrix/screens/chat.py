"""Main chat screen: rooms on the left, timeline + composer on the right."""

from __future__ import annotations

import asyncio
import re
import time
import webbrowser

from ..events import ImageEvent, MessageEvent, Room
from rich.markup import escape
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static

from .. import themes
from ..cache import MessageCache
from ..config import recovery_has_verifier
from ..dialogs.invite import InviteDialog
from ..dialogs.recovery import RecoveryDialog
from ..dialogs.sas import SasDialog
from ..dialogs.search import SearchDialog
from ..formatting import (
    _URL_RE,
    _format_time,
    _fuzzy_score,
    _inline_markdown,
    _sender_color,
    MessageBlock,
    TimelineContext,
    TimelineEntry,
    body_mentions_user,
    format_date_separator,
    format_timeline_blocks,
    highlight_mentions,
    annotation_of,
    reaction_counts,
    reaction_summary,
    reply_fallback,
    reply_target_of,
    strip_reply_fallback,
)
from ..image_renderer import format_image_message, is_image_message, render_image
from ..matrix_client import ShelltrixClient
from ..notifications import notify
from ..sidebar import (
    _sidebar_room_markup,
    _sidebar_session_markup,
    box_header,
    branch,
)
from ..widgets import COLLAPSED_LINES, MessageView, _SendButton
from .login import LoginScreen

# sync state → (label, color) mapping for the header. The colors are frozen
# constants (not tied to the theme); only the "online" row switches to
# themes.accent() at display time.
SYNC_LABELS = {
    "connecting": ("offline", "#ff6f6f"),
    "syncing": ("syncing…", "#d7a85f"),
    "online": ("online", "#88c285"),
    "offline": ("off-line", "#ff6f6f"),
    "reconnecting": ("reconnecting…", "#d7a85f"),
}

# Sentinel stating that all of a room's history is already loaded
# (may be empty until a first page has been fetched).
_HISTORY_END = object()

# Timeline indent width (author gutter), and the fallback width used to
# compute visible lines before the first layout.
_TIMELINE_INDENT_COLS = 8
_FALLBACK_TIMELINE_WIDTH = 80


class ChatScreen(Screen):
    """Main interface: rooms left, timeline + composer right."""

    BINDINGS = [
        ("ctrl+r", "focus_rooms", "Rooms"),
        ("ctrl+l", "focus_input", "Compose"),
        ("ctrl+k", "clear_screen", "Clear"),
        ("ctrl+d", "toggle_sidebar", "Sidebar"),
        ("ctrl+f", "search", "Search"),
        ("pageup", "timeline_history", "Scroll up / History"),
        ("pagedown", "timeline_down", "Scroll down"),
    ]

    def __init__(self, client: ShelltrixClient) -> None:
        super().__init__()
        self.client = client
        self.active_room_id: str | None = None
        self.unread: dict[str, int] = {}
        # Local SQLite cache: persists timeline messages per room, scoped
        # to the current account (self.client.user_id).
        self._cache = MessageCache(self.client.user_id)
        # Unread direct mentions per room (message @us, unread): used to show
        # a distinct indicator ('@') in the room list.
        self.mentions: dict[str, int] = {}
        self.message_log: dict[str, list[TimelineEntry]] = {}
        # Timeline grouping state per room (last sender / time for the time
        # separators and the blocks).
        self._timeline_ctx: dict[str, TimelineContext] = {}
        # event_id → name index per room: used to resolve the message quoted
        # by a reply ("in reply to X"). Empty until a room has been
        # rendered.
        self._reply_index: dict[str, dict[str, str]] = {}
        # Reactions: (room_id, message event_id) → {sender: emoji}. We store
        # the AUTHOR rather than a counter, which makes duplicates
        # structurally impossible (the spec allows only one reaction per
        # person per message: changing emoji replaces the old one) and lets
        # history reactions be loaded as well as live ones. Volatile by
        # choice (not persisted): they are rebuilt by re-reading the history
        # page when the room is opened.
        self._reactions: dict[tuple[str, str], dict[str, str]] = {}
        # Last event_id received per room: used by the /react command.
        self.last_event_id: dict[str, str] = {}
        # Last URL seen per room: used by the "open the link" command.
        self.last_link: dict[str, str] = {}
        # Autocompletion of slash commands (fuzzy list under the composer).
        self._suggestions_active = False
        self._suggestion_index = 0
        self._suggestion_commands: list[str] = []
        # Autocompletion of @username / #room mentions.
        self._mention_active = False
        self._mention_index = 0
        self._mention_type: str | None = None  # "user" or "room"
        self._mention_query: str = ""
        self._mention_start: int = 0
        self._mention_items: list[tuple[str, str]] = []  # (display, to insert)
        # Observed sync token (SESSION sidebar): wall time of the last
        # next_batch change, to display "Refresh Xs ago".
        self._last_nb: str | None = None
        self._nb_time: float | None = None
        # Status timer (sync/clock) — stored for cleanup on unmount.
        self._status_timer: Timer | None = None
        # Typing indicators (server-authoritative: every event holds the
        # COMPLETE list of users currently typing).
        self._typing_users: dict[str, set[str]] = {}
        self._typing_last_seen: dict[str, float] = {}
        # Server history (scrollback): pagination token per room towards
        # OLDER messages (None = nothing left to load), and rooms being
        # loaded (to avoid concurrent requests).
        self._history_token: dict[str, str | None] = {}
        self._loading_history: set[str] = set()
        # Last displayed line per room (index in `message_log`): used to
        # know whether to scroll_end on a re-render.
        self._at_bottom: dict[str, bool] = {}
        # Sidebar widths kept at the last resize: the title frames (┌───┐)
        # are drawn in markup, so we must know the available width to
        # redraw them at the right size.
        self._left_width = 30
        self._right_width = 30
        # Last markup written into the "ROOMS" frame: avoids repainting
        # the widget on every tick when it has not changed.
        self._room_header_markup: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Static("No room selected", id="room-title")
            yield Static("● offline", id="sync-status")
            yield Static("", id="clock")
        with Horizontal(id="main"):
            with Vertical(id="room-panel"):
                yield Static("◆ shelltrix", id="brand-sidebar")
                yield Static("", id="room-list-header")
                yield ListView(id="room-list")
            with Vertical(id="chat-area"):
                yield VerticalScroll(id="timeline")
                yield ListView(id="suggestion-list")
                yield Static("", id="typing-status")
                with Horizontal(id="input-bar"):
                    yield Static(">", id="input-prompt")
                    yield Input(
                        placeholder="Select a room to chat",
                        id="composer",
                        disabled=True,
                    )
                    yield _SendButton("→", id="composer-send")
            with Vertical(id="sidebar"):
                yield Static("", id="sb-room")
                yield Static("", id="sb-session")
        yield Footer()

    async def on_mount(self) -> None:
        self.client.on_message = self._handle_incoming_message
        self.client.on_image = self._handle_incoming_image
        self.client.on_typing = self._handle_typing
        self.client.on_invite = self._show_invite_dialog
        self.client.on_sas_request = self._show_sas_dialog
        self.client.on_send_error = self._on_send_error
        self.client.on_reaction = self._handle_reaction
        # Wired BEFORE start(): the first sync runs as a background task, so
        # the callback must be in place to not miss the publication of the
        # room list.
        self.client.on_first_sync = self._on_first_sync
        self._status_timer = self.set_interval(1.0, self._tick_status)
        self._tick_status()  # "syncing…" during the first sync
        # start() is non-blocking (the sync runs in the background): the
        # screen shows up right away, the list fills as soon as it arrives.
        self.client.start()
        self._refresh_room_list()
        timeline = self.query_one("#timeline", VerticalScroll)
        timeline.mount(
            Static(
                "\n[dim]· · ·  Pick a room from the list to start chatting  · · ·[/dim]"
            )
        )

    async def _on_first_sync(self) -> None:
        """First sync succeeded: the room list is finally populated."""
        if not self.is_mounted:
            return
        self._refresh_room_list()
        self._refresh_sidebar()

    async def on_unmount(self) -> None:
        """Stops the status timer and closes the client cleanly: closing
        encrypts the E2EE store at rest (H4)."""
        if self._status_timer is not None:
            self._status_timer.stop()
            self._status_timer = None
        self._cache.close()
        await self.client.stop()

    def _set_composer_enabled(self, enabled: bool) -> None:
        """Locks/enables the composer depending on whether a room is open."""
        composer = self.query_one("#composer", Input)
        composer.disabled = not enabled
        # Leading space: when the input is empty and focused, the cursor
        # (block) renders on that space and not on the first character of
        # the placeholder — avoids the "Iype a message…" visual glitch.
        composer.placeholder = " Type a message…" if enabled else "Select a room to chat"
        if not enabled:
            composer.value = ""
            if self._suggestions_active:
                self._hide_suggestions()
            if self._mention_active:
                self._hide_mention_suggestions()
        else:
            self._update_suggestions(composer.value)
        # First device without a recovery key: we invite the user to create
        # one to restore the session (E2EE keys) on another machine.
        if not recovery_has_verifier():
            self.app.notify(
                "Run /recovery to back up your session recovery key",
                title="Encryption",
            )

    async def _show_sas_dialog(
        self,
        transaction_id: str,
        user_id: str,
        device_id: str,
        emojis: list[tuple[str, str]],
    ) -> None:
        await self.app.push_screen(
            SasDialog(self.client, transaction_id, user_id, device_id, emojis)
        )

    def _tick_status(self) -> None:
        accent = themes.accent()
        label, color = SYNC_LABELS.get(
            self.client.sync_state, ("idle", themes.muted())
        )
        if self.client.sync_state == "online":
            color = accent
        self.query_one("#sync-status", Static).update(f"[{color}]●[/{color}] {label}")
        self.query_one("#clock", Static).update(time.strftime("%H:%M"))
        self._refresh_room_header()
        self._refresh_sidebar()
        self._purge_typing()

    def _purge_typing(self) -> None:
        """Purges expired typing (safety net, 5s without an event)."""
        now = time.monotonic()
        expired = [
            rid
            for rid, last_seen in self._typing_last_seen.items()
            if now - last_seen > 5.0
        ]
        for rid in expired:
            self._typing_users.pop(rid, None)
            self._typing_last_seen.pop(rid, None)
        if expired and self.active_room_id in expired:
            self._refresh_typing_display()

    async def _handle_typing(self, room_id: str, user_ids: list[str]) -> None:
        """Handles a typing event (server-authoritative, full list)."""
        own_id = self.client.user_id
        typing = {uid for uid in user_ids if uid != own_id}
        self._typing_users[room_id] = typing
        self._typing_last_seen[room_id] = time.monotonic()
        if room_id == self.active_room_id:
            self._refresh_typing_display()

    def _refresh_typing_display(self) -> None:
        """Updates the typing widget for the current room."""
        typing = self._typing_users.get(self.active_room_id, set())
        widget = self.query_one("#typing-status", Static)
        if not typing:
            widget.update("")
            widget.styles.display = "none"
            return
        names = []
        rooms = self.client.rooms()
        room = rooms.get(self.active_room_id)
        for uid in typing:
            if room is not None:
                name = room.user_name(uid) or uid
            else:
                name = uid
            names.append(name)
        if len(names) == 1:
            text = f"{names[0]} is typing…"
        elif len(names) == 2:
            text = f"{names[0]} and {names[1]} are typing…"
        else:
            text = f"{len(names)} people are typing…"
        widget.update(f"[dim]{escape(text)}[/dim]")
        widget.styles.display = "block"

    def _panel_content_width(self, selector: str, fallback: int) -> int:
        """Text columns available in a sidebar panel.

        Title frames are drawn in markup: they must follow the REAL width
        of the content area (panel padding and borders deducted by the
        layout), otherwise the frame's right border overflows onto the
        panel's. The fallback covers the first layout pass, when the size
        is not yet known.
        """
        try:
            width = self.query_one(selector).content_size.width
        except NoMatches:
            return fallback
        return width if width > 4 else fallback

    def _redraw_frames(self) -> None:
        """Redraws both sidebar frames (after layout)."""
        self._refresh_room_header()
        self._refresh_sidebar()

    def _refresh_sidebar(self) -> None:
        """Re-synthesizes both context panels (Room / Session)."""
        room = None
        if self.active_room_id:
            room = self.client.rooms().get(self.active_room_id)
        own_id = getattr(getattr(self.client, "client", None), "user_id", None) or ""
        # Both frames share the width of #sb-room: without its own
        # border, SESSION has exactly the same space available.
        inner = self._panel_content_width("#sb-room", self._right_width - 2)
        self.query_one("#sb-room", Static).update(
            _sidebar_room_markup(room, own_id, inner)
        )
        nb = self.client.next_batch
        nb = nb if isinstance(nb, str) else None
        now = time.time()
        if nb:
            if nb != self._last_nb:
                self._last_nb = nb
                self._nb_time = now
        self.query_one("#sb-session", Static).update(
            _sidebar_session_markup(
                self.client.sync_state, nb, self._nb_time, now, inner
            )
        )

    def _refresh_room_header(self) -> None:
        """Redraws the "ROOMS" title frame at the current width.

        Called on every tick: the frame colors are frozen in the markup, so
        it must be regenerated to follow `/theme`. The cache avoids the
        repaint when nothing changed.
        """
        inner = self._panel_content_width("#room-list-header", self._left_width - 2)
        markup = "\n".join(box_header("ROOMS", inner))
        if markup == self._room_header_markup:
            return
        self._room_header_markup = markup
        self.query_one("#room-list-header", Static).update(markup)

    def action_toggle_sidebar(self) -> None:
        self.query_one("#sidebar").display = not self.query_one("#sidebar").display

    @staticmethod
    def _unread_badge(count: int) -> str:
        """Unread badge label: '1'…'99', capped at '99+'."""
        return "99+" if count > 99 else str(count)

    def _refresh_room_list(self) -> None:
        room_list = self.query_one("#room-list", ListView)
        room_list.clear()
        self._refresh_room_header()
        accent = themes.accent()
        ordered = sorted(
            self.client.rooms().items(),
            key=lambda kv: (
                self.unread.get(kv[0], 0) == 0,  # unread rooms first
                (kv[1].display_name or kv[0]).lower(),
            ),
        )
        for index, (room_id, room) in enumerate(ordered):
            name = room.display_name or room_id
            unread = self.unread.get(room_id, 0)
            mentions = self.mentions.get(room_id, 0) > 0
            is_active = room_id == self.active_room_id
            has_notif = (unread or mentions) and not is_active
            if has_notif:
                name_label = Label(
                    f"[bold][{accent}]{escape(name)}[/{accent}][/bold]",
                    classes="room-name",
                )
            else:
                name_label = Label(escape(name), classes="room-name")
            if mentions:
                badge = Label("@", classes="room-badge mention")
            elif unread:
                badge = Label(self._unread_badge(unread), classes="room-badge")
            else:
                badge = None
            # Tree branch: the last room closes the branch (└─).
            stem = Label(branch(index == len(ordered) - 1), classes="room-stem")
            if badge is not None:
                row = Horizontal(stem, name_label, badge, classes="room-row")
            else:
                row = Horizontal(stem, name_label, classes="room-row")
            item = ListItem(row)
            item.data_room_id = room_id  # type: ignore[attr-defined]
            if is_active:
                item.add_class("active")
            room_list.append(item)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        room_id = getattr(event.item, "data_room_id", None)
        if room_id is None:
            return
        self.active_room_id = room_id
        self.unread[room_id] = 0
        self.mentions[room_id] = 0
        # If the room has no in-memory content for this session yet, we
        # restore what was cached (instant display, offline-ish). The
        # server scrollback then fills in the most recent messages.
        if room_id not in self.message_log:
            self.message_log[room_id] = self._cache.load_entries(room_id)
        room = self.client.rooms().get(room_id)
        title = (room.display_name or room_id) if room else room_id
        self.query_one("#room-title", Static).update(f"[bold]{escape(title)}[/bold]")
        self._refresh_room_list()
        self._render_timeline(room_id, scroll_end=True)
        self._refresh_sidebar()
        self._refresh_typing_display()
        self._set_composer_enabled(True)
        self.query_one("#composer", Input).focus()
        # Load a first batch of server history so a recent room (or one
        # empty on the client side) still shows messages.
        self.call_after_refresh(self._schedule_history_load)

    def _render_timeline(self, room_id: str, *, scroll_end: bool = True) -> None:
        """Re-renders a room's whole timeline from its structured entries.

        Always starts from a fresh grouping context: the stored history is
        grouped from zero for a coherent result (messages may have arrived
        or scrollback may have been prepended). One widget per message (not
        a stream of lines): that is what allows targeting a message for
        scrolling, a reaction or a reply.
        """
        timeline = self.query_one("#timeline", VerticalScroll)
        entries = self.message_log.get(room_id, [])
        ctx = TimelineContext()
        blocks, ctx = format_timeline_blocks(entries, ctx, header_for=self._header_for)
        self._timeline_ctx[room_id] = ctx
        self._reply_index.pop(room_id, None)
        widgets: list[Widget] = []
        for block in blocks:
            widgets.extend(self._prefix_widgets(block))
            widgets.append(self._message_widget(room_id, block))
        timeline.remove_children()
        timeline.mount(*widgets)
        if scroll_end:
            timeline.scroll_end(animate=False)

    def _prefix_widgets(self, block: MessageBlock) -> list[Widget]:
        """Separators (date, silence) preceding a block, if any."""
        widgets: list[Widget] = []
        if block.date_before:
            widgets.append(Static("", classes="tl-gap"))
            widgets.append(
                Static(format_date_separator(block.entry.time_ms), classes="tl-sep")
            )
        elif block.gap_before:
            # The day separator makes the silence line redundant: a day
            # change necessarily implies a silence of more than 5 min, so
            # we only show a single marker.
            widgets.append(Static("", classes="tl-gap"))
            widgets.append(Static(self._time_gap_line(block.entry), classes="tl-sep"))
        return widgets

    @staticmethod
    def _time_gap_line(entry: TimelineEntry) -> str:
        ts = f"{entry.timestamp} " if entry.timestamp else ""
        return f"[dim]{ts}{'─' * 36}[/dim]"

    def _message_widget(self, room_id: str, block: MessageBlock) -> MessageView:
        """Builds a message's widget (reactions + fallback if needed).

        The body is passed as-is: its gutter indent is applied by
        `MessageView` as Padding, which keeps it on the continuation lines
        after wrapping (indenting the string would not reach them).
        """
        return MessageView(
            block,
            reactions=self._reactions_markup(room_id, block.entry.event_id),
            collapsed=self._is_long(block),
        )

    def _is_long(self, block: MessageBlock) -> bool:
        """A message is folded when it exceeds the VISIBLE lines threshold.

        We measure the lines as they will be rendered, so taking the
        wrapping at the terminal width into account: counting `\n` alone
        would ignore every long message without line breaks — the most
        common case in a chat, and precisely the one that overflows. The
        Rich markup of mentions is removed from the count, otherwise
        `@bob` would count for its tags.
        """
        plain = re.sub(r"\[/?[^\[\]]+\]", "", block.entry.body)
        width = max(self.timeline_width - _TIMELINE_INDENT_COLS, 20)
        rendered = sum(
            max(1, -(-len(part) // width)) for part in plain.split("\n")
        )
        return rendered >= COLLAPSED_LINES

    @property
    def timeline_width(self) -> int:
        """Usable timeline width, with a safe fallback before layout."""
        try:
            width = self.query_one("#timeline", VerticalScroll).content_size.width
        except NoMatches:
            return _FALLBACK_TIMELINE_WIDTH
        return width if width > 20 else _FALLBACK_TIMELINE_WIDTH

    def open_message(self, room_id: str, event_id: str) -> None:
        """Opens a room and positions on a specific message.

        Used by message search: switches to the room, restores its
        history from the cache, then scrolls to the target's position
        (approximated by its rank in the log — every entry renders at
        least one line).
        """
        self.active_room_id = room_id
        self.unread[room_id] = 0
        self.mentions[room_id] = 0
        if room_id not in self.message_log:
            self.message_log[room_id] = self._cache.load_entries(room_id)
        room = self.client.rooms().get(room_id)
        title = (room.display_name or room_id) if room else room_id
        self.query_one("#room-title", Static).update(f"[bold]{escape(title)}[/bold]")
        self._refresh_room_list()
        self._render_timeline(room_id, scroll_end=False)
        timeline = self.query_one("#timeline", VerticalScroll)
        # PRECISE scroll to the targeted message: we find the widget by
        # its event_id. (The old RichLog only had a stream of lines, hence
        # the approximation by rank in the log.)
        target = next(
            (
                w
                for w in timeline.query(MessageView)
                if w.event_id == event_id
            ),
            None,
        )
        if target is not None:
            timeline.call_after_refresh(
                lambda: timeline.scroll_to_widget(target, animate=False)
            )
        else:
            timeline.scroll_end(animate=False)
        self._refresh_sidebar()
        self._refresh_typing_display()
        self._set_composer_enabled(True)
        self.query_one("#composer", Input).focus()
        self.call_after_refresh(self._schedule_history_load)

    def action_timeline_down(self) -> None:
        if not self.active_room_id:
            return
        timeline = self.query_one("#timeline", VerticalScroll)
        timeline.scroll_down(animate=False)

    def action_timeline_history(self) -> None:
        """Scrolls the timeline up; at the top, loads older messages."""
        if not self.active_room_id:
            return
        timeline = self.query_one("#timeline", VerticalScroll)
        at_top = timeline.scroll_y <= 0
        if at_top:
            self._schedule_history_load()
        else:
            timeline.scroll_up(animate=False)

    def _schedule_history_load(self) -> None:
        room_id = self.active_room_id
        if not room_id or room_id in self._loading_history:
            return
        if self._history_token.get(room_id) == _HISTORY_END:
            return  # everything already loaded
        self._loading_history.add(room_id)
        self.run_worker(self._load_older_history(room_id), exclusive=True, group="history")

    async def _load_older_history(self, room_id: str) -> None:
        """Fetches older messages (scrollback) and prepends them.

        On the first call without a token, we take the most recent page
        (start=None); then we go back page by page into the past. Entries
        received as duplicates (already present from sync) are deduplicated
        by event_id. After insertion we re-render and preserve the scroll
        position.
        """
        try:
            if not self.active_room_id or self.active_room_id != room_id:
                return
            token = self._history_token.get(room_id)
            page = await self.client.room_messages(room_id, start=token, limit=40)
            if page is None or not page.events:
                if page is not None:
                    self._history_token[room_id] = _HISTORY_END
                return
            timeline = self.query_one("#timeline", VerticalScroll)
            prev_scroll = timeline.scroll_y
            events = list(page.events)
            # Reactions travel in the same page as the messages: we
            # harvest them before building the entries, otherwise the
            # history would display without any counter.
            self._harvest_reactions(room_id, events)
            entries = self._entries_from_events(room_id, events)
            existing_ids = {e.event_id for e in self.message_log.get(room_id, [])}
            added = [
                e for e in entries if e.event_id and e.event_id not in existing_ids
            ]
            if not added:
                # Nothing new: probably reached the end of the history.
                self._history_token[room_id] = _HISTORY_END
                return
            current = self.message_log.get(room_id, [])
            self.message_log[room_id] = added + current
            self._cache.upsert_entries(room_id, added)
            self._render_timeline(room_id, scroll_end=False)
            added_ids = {e.event_id for e in added}
            self.call_after_refresh(
                lambda: self._restore_scroll_after_prepend(timeline, prev_scroll, added_ids)
            )
            self._history_token[room_id] = (
                page.end or _HISTORY_END
            )
        finally:
            self._loading_history.discard(room_id)

    def _restore_scroll_after_prepend(
        self,
        timeline: VerticalScroll,
        prev_scroll: int,
        added_ids: set[str],
    ) -> None:
        """Restores the scroll after prepending older history.

        The pre-existing content slid down by as many lines as the new
        messages occupy. We therefore offset by the ACTUALLY RENDERED
        HEIGHT of the added widgets: the old approximation "+ len(added)"
        assumed one line per message, which was wrong as soon as a message
        took several lines (or when a separator was interleaved). Only
        measurable after the layout pass, hence the caller's
        `call_after_refresh`.
        """
        delta = sum(
            w.outer_size.height
            for w in timeline.query(MessageView)
            if w.event_id in added_ids
        )
        timeline.scroll_to(
            y=min(prev_scroll + delta, timeline.max_scroll_y),
            animate=False,
        )

    def _entries_from_events(
        self, room_id: str, events: list[MessageEvent | ImageEvent]
    ) -> list[TimelineEntry]:
        """Converts normalized history events into TimelineEntry.

        Handles text/emote/notice messages and images (placeholder).
        Returns the entries in ascending chronological order (oldest
        first), ready to be prepended.
        """
        own_id = self.client.user_id
        me = self.client.user_id

        out: list[TimelineEntry] = []
        for ev in events:
            # An `m.reaction` annotation is a message with an empty body:
            # without this filter, every history reaction would add a
            # BLANK line to the timeline.
            if annotation_of(getattr(ev, "source", {}).get("content", {}))[0]:
                continue
            try:
                raw_ts = getattr(ev, "server_timestamp", 0) or 0
                if isinstance(ev, ImageEvent):
                    filename = ev.body or "image"
                    own = ev.sender == me
                    out.append(
                        TimelineEntry(
                            sender=ev.sender,
                            display_name=(
                                "You"
                                if own
                                else (self._room_name(room_id, ev.sender))
                            ),
                            is_own=own,
                            time_ms=raw_ts,
                            body=f"[image: {filename}]",
                            event_id=getattr(ev, "event_id", "") or "",
                            msgtype="m.image",
                            is_image=True,
                            image_hint=filename,
                            timestamp=_format_time(raw_ts),
                        )
                    )
                elif isinstance(ev, MessageEvent):
                    body = getattr(ev, "body", "") or ""
                    own = ev.sender == me
                    reply_to = reply_target_of(
                        getattr(ev, "source", {}).get("content", {})
                    )
                    cited_id, cited_name = self._event_names(room_id).get(
                        reply_to, ("", "")
                    )
                    if reply_to:
                        body = strip_reply_fallback(body, cited_id)
                    out.append(
                        TimelineEntry(
                            sender=ev.sender,
                            display_name=(
                                "You" if own else (self._room_name(room_id, ev.sender))
                            ),
                            is_own=own,
                            time_ms=raw_ts,
                            body=highlight_mentions(_inline_markdown(body), own_id),
                            event_id=getattr(ev, "event_id", "") or "",
                            msgtype=getattr(ev, "msgtype", "m.text") or "m.text",
                            has_mention=body_mentions_user(body, own_id),
                            timestamp=_format_time(raw_ts),
                            reply_to_event_id=reply_to,
                            reply_to_name=cited_name,
                        )
                    )
            except Exception:
                continue
        out.sort(key=lambda e: e.time_ms)
        return out

    def _room_name(self, room_id: str, sender: str) -> str:
        """Display name of a sender in a room (resolved or raw)."""
        room = self.client.rooms().get(room_id)
        if room is None:
            return sender
        try:
            return room.user_name(sender) or sender
        except Exception:
            return sender

    def _header_for(self, entry: TimelineEntry) -> str:
        """Rich markup of the block header: time in the gutter + author.

        The time occupies a fixed column on the left and the name starts
        exactly on the message body column (8 = 5 for "HH:MM" + 3 spaces),
        so the text does not shift when the timestamp is added. It is only
        rendered on headers: consecutive messages from the same author
        stay muted, and the time is the one at the start of the block —
        repeating it on every line would be the noise grouping removes.
        """
        ts = escape(entry.timestamp) if entry.timestamp else "--:--"
        stamp = f"[dim]{ts}[/dim]   "
        if entry.is_own:
            accent = themes.accent()
            return f"{stamp}[bold][{accent}]› You[/{accent}][/bold]"
        color = _sender_color(entry.sender)
        name = escape(entry.display_name or entry.sender)
        return f"{stamp}[{color}]‹ {name}[/{color}]"

    # -- Reply and reaction resolution -------------------------------

    def _event_names(self, room_id: str) -> dict[str, tuple[str, str]]:
        """`event_id` → (user_id, display name) index of a room.

        Serves two things that do not derive from one another:
          - the `<@user_id>` fallback prefix we set on send, which requires
            the IDENTIFIER and not the display name (that was the bug: we
            looked for "Tim" where the body contained `<@tim:hs> `);
          - the "in reply to X" label, which wants the readable name.
        Rebuilt on every full render (prepended history changes the
        content), then incrementally on append.
        """
        idx = self._reply_index.get(room_id)
        if idx is None:
            idx = {
                e.event_id: (e.sender, e.display_name or e.sender)
                for e in self.message_log.get(room_id, [])
                if e.event_id
            }
            self._reply_index[room_id] = idx
        return idx

    def _reactions_markup(self, room_id: str, event_id: str) -> str:
        return reaction_summary(
            reaction_counts(self._reactions.get((room_id, event_id), {}))
        )

    def on_resize(self, event: events.Resize) -> None:
        """Progressive sidebar widths according to the terminal width.

        Grid targeted at 1920×1080 (~210 columns): ~30 cells on each
        side, the chat takes all the rest. Below that we shrink in steps
        so the screen is never saturated; under 100 columns we hide the
        right sidebar (always replaceable by the existing shortcut).
        """
        w = event.size.width
        if w >= 200:
            left = right = 30
        elif w >= 150:
            left = right = 26
        elif w >= 110:
            left = right = 22
        else:
            left = right = 20
        self._left_width = left
        self._right_width = right
        try:
            self.query_one("#room-panel").styles.width = left
            sb = self.query_one("#sidebar")
            sb.styles.width = right
            sb.display = sb.display if w >= 100 else False
            # Title frames are text: they follow the width, measured
            # once the layout has run (hence call_after_refresh).
            self.call_after_refresh(self._redraw_frames)
        except Exception:
            pass

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "composer" or self.active_room_id is None:
            return
        await self._dispatch_compose(event.input)

    async def _dispatch_compose(self, composer: Input) -> None:
        # Enter while autocompletion is open: we complete the
        # highlighted command instead of executing the input.
        if self._suggestions_active:
            self._accept_suggestion()
            return
        if self._mention_active:
            self._accept_mention_suggestion()
            return
        body = composer.value.strip()
        if not body:
            return
        composer.value = ""
        if body.startswith("/"):
            await self._run_slash_command(self.active_room_id, body)
            return
        await self.client.send_message(self.active_room_id, body)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------
    SLASH_HELP = {
        "/me <text>": "send an action (m.emote, rendered in italic)",
        "/react <emoji>": "react to the last received message",
        "/reactions [event_id]": "reload a message's reactions from the server",
        "/reply <text>": "reply to the last received message",
        "/join <#alias>": "join a room by alias",
        "/sendimg <path>": "send an image from disk",
        "/search <text>": "search the local message history",
        "/quit [farewell]": "leave the room (optional farewell message)",
        "/recovery": "show or regenerate the E2EE session recovery key",
        "/theme": "switch theme (OpenCode Zen / Matrix Green)",
        "/help": "show this help",
    }
    # Canonical commands (without usage) for fuzzy completion.
    SLASH_COMMANDS: list[str] = [usage.split()[0] for usage in SLASH_HELP]
    SLASH_DESC = {
        "/me": "send an action",
        "/react": "react to the last message",
        "/reactions": "reload reactions",
        "/reply": "reply to the last message",
        "/join": "join a room",
        "/sendimg": "send an image",
        "/search": "search local history",
        "/quit": "leave the room",
        "/recovery": "session recovery key",
        "/theme": "switch theme",
        "/help": "show this help",
    }

    async def _run_slash_command(self, room_id: str, raw: str) -> None:
        parts = raw.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help":
            self._cmd_help()
        elif cmd == "/theme":
            self.app.cycle_theme()  # type: ignore[attr-defined]
        elif cmd == "/me":
            if not arg:
                self.app.notify("/me <text>: a text is required", severity="error")
                return
            await self.client.send_emote(room_id, arg)
        elif cmd == "/react":
            if not arg:
                self.app.notify("/react <emoji>: provide an emoji", severity="error")
                return
            event_id = self.last_event_id.get(room_id)
            if not event_id:
                self.app.notify("No message received to react to in this room")
                return
            await self.client.react_to(room_id, event_id, arg)
        elif cmd == "/reactions":
            await self._cmd_refresh_reactions(room_id, arg)
        elif cmd == "/reply":
            if not arg:
                self.app.notify("/reply <text>: a reply text is required", severity="error")
                return
            event_id = self.last_event_id.get(room_id)
            if not event_id:
                self.app.notify("No message received to reply to in this room")
                return
            # Reply to the LAST received message, like /react. We find its
            # author via the index to set the fallback prefix required by
            # the Matrix spec (clients that do not render the relation must
            # still see who we are replying to).
            cited_id, _cited_name = self._event_names(room_id).get(event_id, ("", ""))
            await self.client.send_message(
                room_id,
                reply_fallback(arg, cited_id),
                reply_to_event_id=event_id,
            )
        elif cmd == "/join":
            if not arg or not arg.startswith("#"):
                self.app.notify("/join <#alias>: provide a room alias", severity="error")
                return
            await self.client.join_room(arg)
            await self._refresh_room_list_async()
        elif cmd == "/sendimg":
            if not arg:
                self.app.notify("/sendimg <path>: provide a file path", severity="error")
                return
            await self.client.send_image(room_id, arg)
        elif cmd == "/search":
            self.app.push_screen(SearchDialog(self, initial_query=arg))  # type: ignore[attr-defined]
        elif cmd == "/quit":
            await self.client.part_room(room_id, arg or None)
            self.active_room_id = None
            self.query_one("#room-title", Static).update(
                "[bold]No room selected[/bold]"
            )
            self._set_composer_enabled(False)
            self._refresh_room_list()
            self._refresh_sidebar()
        elif cmd == "/recovery":
            self.app.push_screen(RecoveryDialog(self))  # type: ignore[attr-defined]
        else:
            self.app.notify(
                f"Unknown command: {cmd} (type /help)",
                severity="error",
            )

    def _cmd_help(self) -> None:
        accent = themes.accent()
        lines = [f"[bold][{accent}]/{cmd}[/{accent}]  {desc}" for cmd, desc in self.SLASH_HELP.items()]
        self.app.notify(
            f"[bold]Slash commands[/bold]\n" + "\n".join(lines),
            title="Commands",
            timeout=8,
        )

    # ------------------------------------------------------------------
    # Fuzzy autocompletion of slash commands
    # ------------------------------------------------------------------
    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "composer":
            cursor = event.input.cursor_position
            self._update_suggestions(event.value, cursor)

    def _matching_commands(self, query: str) -> list[str]:
        scored = [
            (score, cmd)
            for cmd in self.SLASH_COMMANDS
            if (score := _fuzzy_score(query, cmd[1:])) >= 0
        ]
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [cmd for _, cmd in scored]

    def _update_suggestions(self, value: str, cursor: int | None = None) -> None:
        """Updates the suggestions: slash commands or @/# mentions."""
        if cursor is None:
            cursor = len(value)
        # First check @/# mentions (priority over slash commands)
        if not value.startswith("/"):
            self._suggestions_active = False
            self._update_mention_suggestions(value, cursor)
            return
        # Otherwise, slash commands
        self._mention_active = False
        accent = themes.accent()
        lst = self.query_one("#suggestion-list", ListView)
        if not value.startswith("/"):
            self._hide_suggestions()
            return
        self._suggestion_commands = self._matching_commands(value[1:])
        if not self._suggestion_commands:
            self._hide_suggestions()
            return
        lst.clear()
        for cmd in self._suggestion_commands:
            desc = self.SLASH_DESC.get(cmd, "")
            item = ListItem(
                Label(
                    f"[bold][{accent}]{escape(cmd)}[/{accent}][/bold]"
                    f"  [dim]{escape(desc)}[/dim]"
                )
            )
            lst.append(item)
        self._suggestion_index = 0
        lst.index = 0
        self._suggestions_active = True
        lst.styles.display = "block"

    def _hide_suggestions(self) -> None:
        self._suggestions_active = False
        self._suggestion_commands = []
        if not self._mention_active:
            self.query_one("#suggestion-list", ListView).styles.display = "none"

    # ------------------------------------------------------------------
    # Autocompletion of the @username / #room mentions
    # ------------------------------------------------------------------
    def _active_room_users(self) -> list[tuple[str, str]]:
        """List of the current room's members: [(user_id, display_name)].

        We exclude the current user: there is never a need to mention
        yourself (@self mentions make no sense in Matrix, so they are
        skipped)."""
        if self.active_room_id is None:
            return []
        room = self.client.rooms().get(self.active_room_id)
        if room is None:
            return []
        own_id = getattr(self.client, "client", None)
        own_id = getattr(own_id, "user_id", None)
        users: list[tuple[str, str]] = []
        for uid in getattr(room, "users", {}):
            if uid == own_id:
                continue
            name = room.user_name(uid) or uid
            users.append((uid, name))
        users.sort(key=lambda t: t[1].lower())
        return users

    def _all_rooms(self) -> list[tuple[str, str]]:
        """List of all rooms: [(alias_or_id, display_name)]."""
        rooms: list[tuple[str, str]] = []
        for rid, room in self.client.rooms().items():
            alias = getattr(room, "canonical_alias", None)
            name = room.display_name or rid
            if alias:
                rooms.append((alias, name))
            else:
                rooms.append((rid, name))
        rooms.sort(key=lambda t: t[1].lower())
        return rooms

    def _find_mention_start(self, value: str, cursor: int) -> tuple[int, str, str] | None:
        """Detects whether the cursor is inside an @ or # mention.

        Returns (start_position, type, query) or None.
        type = "user" for @, "room" for #.
        """
        if not value or cursor == 0:
            return None
        # Find the closest @ or # before the cursor
        at_pos = value.rfind("@", 0, cursor)
        hash_pos = value.rfind("#", 0, cursor)
        pos = max(at_pos, hash_pos)
        if pos == -1:
            return None
        # Check the mention starts a word (string start or preceded by a space)
        if pos > 0 and value[pos - 1] != " ":
            return None
        # Extract the query (without @ or #)
        mention = value[pos:cursor]
        if not mention:
            return None
        prefix = mention[0]
        query = mention[1:]
        # Only suggest when the query has no space (mention in progress)
        if " " in query:
            return None
        mtype = "user" if prefix == "@" else "room"
        return (pos, mtype, query)

    def _update_mention_suggestions(self, value: str, cursor: int) -> None:
        """Updates the mention suggestion list."""
        found = self._find_mention_start(value, cursor)
        if found is None:
            self._hide_mention_suggestions()
            return
        start, mtype, query = found
        self._mention_start = start
        self._mention_type = mtype
        self._mention_query = query

        if mtype == "user":
            candidates = self._active_room_users()
        else:
            candidates = self._all_rooms()

        # Filter by fuzzy match
        q = query.lower()
        scored: list[tuple[float, str, str]] = []
        for insert, display in candidates:
            score = _fuzzy_score(q, display.lower())
            if score >= 0:
                scored.append((score, display, insert))
        scored.sort(key=lambda t: -t[0])
        self._mention_items = [(d, i) for _, d, i in scored]

        if not self._mention_items:
            self._hide_mention_suggestions()
            return

        accent = themes.accent()
        lst = self.query_one("#suggestion-list", ListView)
        lst.clear()
        for display, insert in self._mention_items[:10]:  # max 10 results
            prefix = "@" if mtype == "user" else "#"
            item = ListItem(
                Label(
                    f"[bold][{accent}]{prefix}{escape(display)}[/{accent}][/bold]"
                    f"  [dim]{escape(insert)}[/dim]"
                )
            )
            lst.append(item)
        self._mention_index = 0
        lst.index = 0
        self._mention_active = True
        lst.styles.display = "block"

    def _hide_mention_suggestions(self) -> None:
        self._mention_active = False
        self._mention_items = []
        self._mention_type = None
        if not self._suggestions_active:
            self.query_one("#suggestion-list", ListView).styles.display = "none"

    def _accept_mention_suggestion(self) -> None:
        if not (0 <= self._mention_index < len(self._mention_items)):
            self._hide_mention_suggestions()
            return
        display, insert = self._mention_items[self._mention_index]
        composer = self.query_one("#composer", Input)
        value = composer.value
        # Replace the mention with the choice + space
        end = self._mention_start + len(self._mention_query) + 1  # +1 for @/#
        new_value = value[:self._mention_start] + insert + " " + value[end:]
        composer.value = new_value
        composer.cursor_position = self._mention_start + len(insert) + 1
        self._hide_mention_suggestions()
        composer.focus()

    def _accept_suggestion(self) -> None:
        commands = self._suggestion_commands
        if not (0 <= self._suggestion_index < len(commands)):
            self._hide_suggestions()
            return
        cmd = commands[self._suggestion_index]
        self._hide_suggestions()
        self._hide_mention_suggestions()
        composer = self.query_one("#composer", Input)
        composer.value = f"{cmd} "
        composer.cursor_position = len(composer.value)
        composer.focus()

    def on_key(self, event: events.Key) -> None:
        if self._mention_active:
            lst = self.query_one("#suggestion-list", ListView)
            total = len(self._mention_items)
            if event.key == "down":
                self._mention_index = min(self._mention_index + 1, total - 1)
                lst.index = self._mention_index
                event.stop()
            elif event.key == "up":
                self._mention_index = max(self._mention_index - 1, 0)
                lst.index = self._mention_index
                event.stop()
            elif event.key == "tab":
                self._accept_mention_suggestion()
                event.stop()
            elif event.key == "escape":
                self._hide_mention_suggestions()
                event.stop()
            return
        if not self._suggestions_active:
            return
        lst = self.query_one("#suggestion-list", ListView)
        total = len(self._suggestion_commands)
        if event.key == "down":
            self._suggestion_index = min(self._suggestion_index + 1, total - 1)
            lst.index = self._suggestion_index
            event.stop()
        elif event.key == "up":
            self._suggestion_index = max(self._suggestion_index - 1, 0)
            lst.index = self._suggestion_index
            event.stop()
        elif event.key == "tab":
            self._accept_suggestion()
            event.stop()
        elif event.key == "escape":
            self._hide_suggestions()
            event.stop()

    async def _refresh_room_list_async(self) -> None:
        # Short delay to give the server time to accept the join before
        # the next refresh.
        await asyncio.sleep(0.3)
        self._refresh_room_list()

    def _append_timeline_entry(self, room_id: str, entry: TimelineEntry) -> None:
        """Stores an entry, renders it incrementally if the room is active.

        The grouping (blocks / time separators) is computed at render time
        via the room's persistent context: consistent with the full
        re-render on room open and amortized O(1) in real time.
        """
        self.message_log.setdefault(room_id, []).append(entry)
        self._cache.upsert_entries(room_id, [entry])
        if entry.event_id:
            self._event_names(room_id)[entry.event_id] = (
                entry.sender,
                entry.display_name or entry.sender,
            )
        if room_id != self.active_room_id:
            return
        ctx = self._timeline_ctx.get(room_id, TimelineContext())
        blocks, ctx = format_timeline_blocks([entry], ctx, header_for=self._header_for)
        self._timeline_ctx[room_id] = ctx
        timeline = self.query_one("#timeline", VerticalScroll)
        widgets: list[Widget] = [*self._prefix_widgets(blocks[0])]
        widgets.append(self._message_widget(room_id, blocks[0]))
        timeline.mount(*widgets)

    def _notify_incoming(
        self,
        room: Room,
        entry: TimelineEntry,
        body: str,
    ) -> None:
        """Increments the unread count and notifies the desktop if needed.

        Only called when the message arrives in an inactive room.
        """
        self.unread[room.room_id] = self.unread.get(room.room_id, 0) + 1
        if entry.has_mention and not entry.is_own:
            self.mentions[room.room_id] = self.mentions.get(room.room_id, 0) + 1
        self._refresh_room_list()
        if not entry.is_own:
            prefix = "@Mention · " if entry.has_mention else ""
            notify(
                room.display_name or room.name or room.room_id,
                f"{prefix}{entry.display_name or entry.sender}",
                body,
            )

    async def _handle_incoming_message(self, room: Room, event: MessageEvent) -> None:
        me = self.client.user_id
        # A reaction is a message with an empty body, and it is routed
        # through the same callback: without this filter, every received
        # emoji would add an empty ghost message to the timeline.
        # It is handled by `_handle_reaction`.
        if annotation_of(getattr(event, "source", {}).get("content", {}))[0]:
            return
        own = event.sender == me
        raw = event.body or ""
        # Reply: we read the relation to know which message is quoted, and
        # strip the fallback prefix we set on send (otherwise our own
        # timeline would show the original glued to the reply).
        reply_to = reply_target_of(getattr(event, "source", {}).get("content", {}))
        cited_id, cited_name = self._event_names(room.room_id).get(reply_to, ("", ""))
        if reply_to:
            # The fallback prefix we set on send carries the AUTHOR OF THE
            # QUOTED MESSAGE: we strip it by its user_id, otherwise our
            # own timeline would show the original glued to the reply.
            raw = strip_reply_fallback(raw, cited_id)
        body = highlight_mentions(_inline_markdown(raw), me)
        # We store the room's last event_id: the /react command refers
        # to it to set a reaction.
        self.last_event_id[room.room_id] = event.event_id
        # and the last URL seen, for "open the last link".
        m = _URL_RE.search(event.body)
        if m:
            self.last_link[room.room_id] = m.group(0)

        entry = TimelineEntry(
            sender=event.sender,
            display_name=(
                "You" if own else (room.user_name(event.sender) or event.sender)
            ),
            is_own=own,
            time_ms=event.server_timestamp or 0,
            body=body,
            event_id=event.event_id or "",
            msgtype=getattr(event, "msgtype", "m.text") or "m.text",
            has_mention=body_mentions_user(event.body, me),
            timestamp=_format_time(event.server_timestamp),
            reply_to_event_id=reply_to,
            reply_to_name=cited_name,
        )
        if room.room_id != self.active_room_id:
            self._notify_incoming(room, entry, event.body)
            self._append_timeline_entry(room.room_id, entry)
            return
        self._append_timeline_entry(room.room_id, entry)

    async def _handle_incoming_image(
        self, room: Room, event: ImageEvent
    ) -> None:
        """Handles the reception of an image."""
        own = event.sender == self.client.user_id
        sender_name = (
            "You" if own else (room.user_name(event.sender) or event.sender)
        )
        filename = event.body or "image"

        # Already resolved by the transport, which knows both the Matrix v3
        # (`url`) and legacy (`file.url`) shapes.
        image_url = event.url

        entry = TimelineEntry(
            sender=event.sender,
            display_name=sender_name,
            is_own=own,
            time_ms=event.server_timestamp or 0,
            body=f"[image: {escape(filename)}]",
            timestamp=_format_time(event.server_timestamp),
            is_image=True,
            image_hint=filename,
        )
        if room.room_id != self.active_room_id:
            self._notify_incoming(room, entry, f"[image: {filename}]")
            self._append_timeline_entry(room.room_id, entry)
            return
        self._append_timeline_entry(room.room_id, entry)

        # Try to display the image inline if the URL is available AND we
        # are in the current room. Rendering is always Textual-safe: no raw
        # write to stdout (which would corrupt the full-screen display).
        if image_url:
            result = format_image_message(
                sender_name,
                image_url,
                filename,
                self.client.creds.access_token,
                self.client.creds.homeserver,
            )
            timeline = self.query_one("#timeline", VerticalScroll)
            if is_image_message(result):
                # Extract the path and display
                parts = result.split(":", 2)
                if len(parts) == 3:
                    local_path = parts[1]
                    img_result = render_image(local_path)
                    timeline.mount(Static(f"        {img_result}"))
            elif result:
                timeline.mount(Static(f"        {result}"))

    async def _handle_reaction(
        self, room: Room, target_id: str, key: str, sender: str
    ) -> None:
        """Records a reaction and refreshes the target message counter.

        Reaction SENT LIVE (sync). History goes through
        `_harvest_reactions`, which builds the same index.
        """
        me = self.client.user_id
        if sender == me:
            # We do not count our own send: the server re-emits it in
            # the sync while we already applied it. Reacting again
            # replaces the previous key of the same author.
            return
        self._record_reaction(room.room_id, target_id, key, sender)
        if room.room_id != self.active_room_id:
            return
        for view in self.query_one("#timeline", VerticalScroll).query(MessageView):
            if view.event_id == target_id:
                view.add_reaction(self._reactions_markup(room.room_id, target_id))
                return

    def _record_reaction(
        self, room_id: str, target_id: str, key: str, sender: str
    ) -> None:
        """Stores a reaction, refreshes its display if the room is seen.

        Single entry point for live and history: both write into the
        same index, so the same `event_id` received twice only counts
        once.
        """
        self._reactions.setdefault((room_id, target_id), {})[sender] = key
        if room_id != self.active_room_id:
            return
        for view in self.query_one("#timeline", VerticalScroll).query(MessageView):
            if view.event_id == target_id:
                view.add_reaction(self._reactions_markup(room_id, target_id))
                return

    def _harvest_reactions(self, room_id: str, events: list) -> int:
        """Fetches the reactions contained in a history page.

        `/messages` returns the FULL timeline: annotations are interleaved
        between the messages they concern. Without this read, all history
        reactivity was lost — counters only appeared after a client
        restart. Returns the number of reactions seen.
        """
        seen = 0
        for ev in events:
            target, key = annotation_of(getattr(ev, "source", {}).get("content", {}))
            sender = getattr(ev, "sender", "") or ""
            if not target or not sender:
                continue
            self._record_reaction(room_id, target, key, sender)
            seen += 1
        return seen

    def _default_reaction_target(self, room_id: str) -> str:
        """Default target message: the most recently DISPLAYED one.

        `last_event_id` only tracks messages received live; after a
        reload from the cache it is empty and the command would target
        nothing. The last element of `message_log`, on the other hand, is
        always the message at the bottom of the timeline.
        """
        entries = self.message_log.get(room_id, [])
        if entries:
            return entries[-1].event_id or ""
        return self.last_event_id.get(room_id, "")

    async def _cmd_refresh_reactions(self, room_id: str, arg: str) -> None:
        """`/reactions [event_id]`: re-reads a message's reactions.

        Paginated history normally provides the reactions, but a message
        coming from the cache (restart, offline) has none. This command
        queries `/relations` for the target message — by default the last
        one received — and updates the counters.
        """
        event_id = arg or self._default_reaction_target(room_id)
        if not event_id:
            self.app.notify("No message to load reactions for", severity="error")
            return
        if event_id not in {e.event_id for e in self.message_log.get(room_id, [])}:
            self.app.notify("That message is not loaded in this room", severity="warning")
            return
        try:
            by_sender = await self.client.fetch_reactions(room_id, event_id)
        except Exception as exc:  # network / old homeserver
            self.app.notify(f"Could not load reactions: {exc}", severity="error")
            return
        bucket = self._reactions.setdefault((room_id, event_id), {})
        # `/relations` returns the CURRENT state: we replace, otherwise
        # a reaction removed since the last load would stay displayed.
        bucket.clear()
        bucket.update(by_sender)
        for view in self.query_one("#timeline", VerticalScroll).query(MessageView):
            if view.event_id == event_id:
                view.add_reaction(self._reactions_markup(room_id, event_id))
                break
        counts = reaction_counts(bucket)
        self.app.notify(
            "Reactions loaded: " + (" ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none")
            if counts
            else "No reactions on that message"
        )

    async def _show_invite_dialog(
        self, room_id: str, room: Room, inviter: str
    ) -> None:
        # We NEVER join a room automatically: an invitation is
        # reported and requires an explicit human decision.
        await self.app.push_screen(InviteDialog(self.client, room_id, room, inviter))

    async def _on_send_error(self, room_id: str, message: str) -> None:
        room = self.client.rooms().get(room_id)
        name = room.display_name if room else room_id
        self.app.notify(
            f"Sending blocked — {escape(name)}: {escape(message)}",
            title="Unverified devices",
        )

    def action_focus_rooms(self) -> None:
        self.query_one("#room-list", ListView).focus()

    def action_focus_input(self) -> None:
        self.query_one("#composer", Input).focus()

    def action_clear_screen(self) -> None:
        """Clears the active room timeline (view + in-memory buffer)."""
        if self.active_room_id is None:
            self.app.notify("Pick a room first")
            return
        self.message_log.pop(self.active_room_id, None)
        self._timeline_ctx.pop(self.active_room_id, None)
        self._reply_index.pop(self.active_room_id, None)
        self._reactions = {k: v for k, v in self._reactions.items() if k[0] != self.active_room_id}
        self.unread[self.active_room_id] = 0
        self._cache.clear_room(self.active_room_id)
        self.query_one("#timeline", VerticalScroll).remove_children()
        self._refresh_room_list()
        self.app.notify("Timeline cleared")

    def action_search(self) -> None:
        """Opens the local message search."""
        self.app.push_screen(SearchDialog(self))

    def action_mark_read(self) -> None:
        if self.active_room_id is None:
            self.app.notify("Pick a room first")
            return
        self.unread[self.active_room_id] = 0
        self._refresh_room_list()

    def action_open_last_link(self) -> None:
        if self.active_room_id is None:
            self.app.notify("Pick a room first")
            return
        url = self.last_link.get(self.active_room_id)
        if not url:
            self.app.notify("No recent link in this room")
            return
        webbrowser.open(url)
        self.app.notify(f"Opening: {url}")

    def action_sync_status(self) -> None:
        label, _ = SYNC_LABELS.get(self.client.sync_state, ("idle", "#565f89"))
        if self.client.sync_state == "online":
            label = "online"
        self.app.notify(
            f"Sync status: [bold]{label}[/bold]",
            title="Matrix",
        )

    def action_insert_slash(self, text: str) -> None:
        composer = self.query_one("#composer", Input)
        composer.value = text
        composer.cursor_position = len(text)
        composer.focus()

    async def logout_and_return_to_login(self) -> None:
        await self.client.logout()
        self.app.notify("Signed out, back to the login screen")
        await self.app.switch_screen(LoginScreen())