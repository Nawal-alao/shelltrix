"""Reusable home-made UI widgets, extracted from `app.py`.

`_SendButton`: flat "→" send button (a clickable Static).
`MessageView`: one timeline message in a dedicated widget — this is what
makes hover, collapsing a long message, reactions and targeted reply
possible (a write-only `RichLog` does not allow it).

No import from `app`, `screens` or `dialogs` — these widgets operate on
their host screen by introspection.
"""

from __future__ import annotations

from rich.padding import Padding
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

from .formatting import MessageBlock

# Number of visible body lines when a message is folded. Beyond that,
# a long message (logs, code snippets, lists) pushes the others off
# screen: folding keeps the conversation readable on active rooms.
COLLAPSED_LINES = 5

# Width of the gutter reserved for the time and the author name. Must stay
# aligned with the equivalent constant of the block rendering.
_GUTTER_COLS = 8


class _SendButton(Static):
    """Flat "→" send button: a clickable Static.

    A real Textual Button enforces `line-pad >= 1` and a 3-line minimum
    height, which makes its label unreadable in the input bar (height 1).
    We therefore use a Static whose click triggers the same send path as
    the Enter key.
    """

    def on_click(self, event: events.Click) -> None:
        event.stop()
        screen = self.screen
        if screen.active_room_id is None:
            return
        self.run_worker(screen._dispatch_compose(screen.query_one("#composer", Input)))


def _gutter(markup: str) -> Padding:
    """Dresses a message body into the timeline gutter.

    The indentation is applied BY THE RENDERING and not by inserting spaces
    in the string: this is the only way to keep it on the continuation
    lines after wrapping to the terminal width (leading spaces in a string
    only apply to the first line).
    """
    return Padding(Text.from_markup(markup), (0, 0, 0, _GUTTER_COLS))


class MessageView(Vertical):
    """A timeline message (header, quote, body, reactions).

    The widget carries the message metadata (`event_id`, `sender`,
    `is_own`): future interactions (reply, react, copy) target by
    `event_id` and no longer have to walk the log again.

    Folding applies to the BODY only — the header (time + author) and the
    reactions always stay visible, otherwise we would lose the information
    of WHO wrote what. The body is truncated by `max-height` (so on real
    rendered lines, not on a character estimate) and a clickable
    "… see more" is added below.
    """

    DEFAULT_CSS = """
    MessageView {
        height: auto;
        width: 1fr;
    }
    MessageView > .msg-head {
        height: auto;
        width: 1fr;
    }
    MessageView > .msg-body {
        height: auto;
        width: 1fr;
    }
    MessageView > .msg-reactions {
        height: auto;
        width: 1fr;
        display: none;
    }
    MessageView > .msg-more {
        height: auto;
        width: 1fr;
        display: none;
    }
    MessageView.-collapsed > .msg-body {
        max-height: $COLLAPSED_LINES;
    }
    MessageView.-collapsed > .msg-more {
        display: block;
    }
    """.replace("$COLLAPSED_LINES", str(COLLAPSED_LINES))

    def __init__(
        self,
        block: MessageBlock,
        *,
        body: str = "",
        reactions: str = "",
        collapsed: bool = False,
    ) -> None:
        super().__init__(classes="timeline-message")
        self.block = block
        self.event_id = block.entry.event_id
        self.sender = block.entry.sender
        self.is_own = block.entry.is_own
        self.time_ms = block.entry.time_ms
        self._body_markup = body or block.entry.body
        self._reactions_markup = reactions
        self._reactions_widget: Static | None = None
        self._collapsed = collapsed

    def compose(self) -> ComposeResult:
        # Everything before the body (author header, quote line)
        # goes into a single widget: none of it is ever truncated.
        head = "\n".join(self.block.lines[:-1])
        reactions = Static(self._reactions_markup, classes="msg-reactions")
        reactions.display = bool(self._reactions_markup)
        self._reactions_widget = reactions
        self.set_class(self._collapsed, "-collapsed")
        yield Static(head, classes="msg-head")
        yield Static(_gutter(self._body_markup), classes="msg-body")
        yield reactions
        yield Static("… see more", classes="msg-more")

    def _set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self.set_class(collapsed, "-collapsed")

    def on_click(self, event: events.Click) -> None:
        """Toggles the body folding when "… see more" is clicked.

        We test `event.widget` (the widget actually under the mouse) and not
        `event.chain`: the latter only contains offsets, not the widgets
        traversed.
        """
        target = event.widget
        if target is not None and "msg-more" in target.classes:
            event.stop()
            self._set_collapsed(False)

    def add_reaction(self, markup: str) -> None:
        """Shows a reactions line below the body.

        The markup is memorized even if the widget does not exist yet: a
        reaction can arrive between the mounting of the message and its
        compose. In that case compose() reads the up-to-date value, so
        nothing is lost.
        """
        self._reactions_markup = markup
        if self._reactions_widget is None:
            return
        self._reactions_widget.update(markup)
        self._reactions_widget.display = bool(markup)