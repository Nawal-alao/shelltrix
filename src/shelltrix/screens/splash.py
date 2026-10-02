"""Splash screen — fixed ASCII block "SHELLTRIX" banner, sober theme
gradient (muted → text, initial letter in primary), cascading logo
reveal, discreet typing of the tagline, auto-transition to login/chat.
Reserved for the very first run (cf. config.first_run_done()).
No hard-coded color: everything comes from the active theme tokens."""

from __future__ import annotations

import asyncio

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Static

from .. import __version__, themes


# --- Animation constants (no color here: violates the absolute rule) ---
_TAGLINE = "secure · private · minimal"   # text typed into the tagline
_FADE_MS = 0.5                            # logo reveal duration
_FADE_STEPS = 12                          # sweep frames (column→column)
_TAG_WAIT = 0.3                           # pause between reveal and typing
_TAG_TICK = 0.05                          # letter appearance rate
_AUTO_MS = 3.0                            # auto-transition (leaves time
                                          # to watch the full sequence)


def _lerp(percent: float, start: tuple[int, int, int], end: tuple[int, int, int]) -> tuple[int, int, int]:
    """Interpolates two RGB colors in 0..255 by `percent` in [0, 1]."""
    return tuple(
        round(a + (b - a) * percent) for a, b in zip(start, end)
    )


def _to_rgb(hex_color: str) -> tuple[int, int, int] | None:
    """'#rrggbb' -> (r, g, b), or None if the value is invalid.

    Theme tokens ($muted, $text, $primary...) always produce valid hex
    values; None is a pure guardrail (no hard-coded color here)."""
    try:
        return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    except (ValueError, TypeError):
        return None


# Fixed "SHELLTRIX" ASCII block (6 lines × 66 columns). It no longer comes
# from a generated font (pyfiglet): hand-drawn, good for both themes.
# Fixed by nature → no shrinking below 66 columns.
_LOGO_ART = r"""███████╗██╗  ██╗███████╗██╗     ██╗  ████████╗██████╗ ██╗██╗  ██╗
██╔════╝██║  ██║██╔════╝██║     ██║  ╚══██╔══╝██╔══██╗██║╚██╗██╔╝
███████╗███████║█████╗  ██║     ██║     ██║   ██████╔╝██║ ╚███╔╝ 
╚════██║██╔══██║██╔══╝  ██║     ██║     ██║   ██╔══██╗██║ ██╔██╗ 
███████║██║  ██║███████╗███████╗███████╗██║   ██║  ██║██║██╔╝ ██╗
╚══════╝╚═╝  ╚═╝╚══════╝╚══════╝╚══════╝╚═╝   ╚═╝  ╚═╝╚═╝╚═╝  ╚═╝ """


_LOGO_COLS = max(len(line) for line in _LOGO_ART.rstrip("\n").split("\n"))


def _splash_art(reveal_cols: int | None = None) -> Text:
    """'SHELLTRIX' banner: sober $muted → $text gradient over the whole
    logo, with no accent letter.

    If `reveal_cols` is given, columns ≥ reveal_cols are rendered in
    $bg (invisible): a clean left-to-right sweep for the reveal."""
    spec = themes.spec()
    start = _to_rgb(spec.muted)
    end = _to_rgb(spec.text)
    art = _LOGO_ART.rstrip("\n")
    lines = art.split("\n")
    if start is None or end is None:
        # Guardrail: invalid theme, render the text with no color.
        out = Text(art)
    else:
        total = sum(len(line) for line in lines)
        out = Text()
        pos = 0.0
        for idx, line in enumerate(lines):
            for ch in line:
                r, g, b = _lerp(pos / total if total else 0.0, start, end)
                out.append(ch, style=f"rgb({r},{g},{b})")
                pos += 1.0
            if idx < len(lines) - 1:
                out.append("\n")

    # Reveal: not-yet-revealed columns → $bg (invisible), already revealed
    # columns keep their gradient.
    if reveal_cols is not None:
        line_start = 0
        for idx, line in enumerate(lines):
            if reveal_cols < len(line):
                out.stylize(
                    spec.bg,
                    line_start + reveal_cols,
                    line_start + len(line),
                )
            line_start += len(line) + 1
    return out


class SplashScreen(Screen):
    """Startup banner: animated ASCII art then entry into the app."""

    BINDINGS = [
        ("escape", "skip", "Skip"),
        ("enter", "skip", "Skip"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._launched = False
        self._animation_started = False
        self._tagline: Static | None = None
        self._typed = 0
        # Animation timers: always stored, always stopped explicitly
        # (.stop()) — never via the callback return value
        # (`return False` is ignored by Textual 8.2.8).
        self._fade_timer: Timer | None = None
        self._wait_timer: Timer | None = None
        self._typing_timer: Timer | None = None
        self._auto_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="splash-wrap"):
            with Vertical(id="splash-group"):
                yield Static(_splash_art(0), id="splash-art")  # animated reveal
                yield Static("", id="splash-sub")  # filled by the typing
                yield Static("enter   continue\nesc     skip", id="splash-hint")
            yield Static("shelltrix // encrypted", id="splash-detail")
            yield Static(f"v{__version__}", id="splash-version")

    def on_mount(self) -> None:
        if self._animation_started:
            return
        self._animation_started = True
        self._adapt_layout()
        self._animate_logo()
        self._auto_timer = self.set_timer(_AUTO_MS, self._launch)

    def on_unmount(self) -> None:
        # Safety net: if the screen is ever popped, no timer survives
        # (idempotent — stop() on an already stopped timer is a
        # no-op).
        self._stop_animation()

    def _adapt_layout(self) -> None:
        """Discreet adaptation to small terminals: Textual CSS has no media
        queries, so compact classes are toggled in Python instead.

        The full block (logo + tagline + hint + margins) fits in 17 lines;
        below that we first tighten the hint ('splash-squeeze', 15-16
        lines), then hide it and tighten the tagline ('splash-tiny', < 15).
        In width, the logo is a fixed 66-column block: below 66 we hide it
        ('-splash-no-logo') rather than truncate its right edge."""
        if self.size.width < 66:
            self.add_class("-splash-no-logo")
        h = self.size.height
        if h >= 17:
            return
        self.add_class("-splash-squeeze")
        if h < 15:
            self.add_class("-splash-tiny")

    def _animate_logo(self) -> None:
        """Reveal of the logo column by column (left → right), then chain
        into the tagline typing.

        Not-yet-revealed columns are rendered in $bg (invisible, same
        technique as the tagline's extinguished cursor): a clean sweep
        rather than a blurry opacity fade. No new timer: _fade_timer
        keeps its existing cadence, only the per-frame rendering logic
        changes. The timer is stored and stopped explicitly on the last
        frame."""
        self._art = self.query_one("#splash-art", Static)
        self._art.update(_splash_art(0))
        self._fade_step = 0
        self._fade_timer = self.set_interval(_FADE_MS / _FADE_STEPS, self._fade_tick)

    def _fade_tick(self) -> None:
        if not self.is_mounted:
            return
        self._fade_step += 1
        # Columns revealed on this frame (out of _LOGO_COLS in total, rounded
        # up to end exactly on the full logo).
        revealed = min(_LOGO_COLS, (_LOGO_COLS * self._fade_step + _FADE_STEPS - 1) // _FADE_STEPS)
        self._art.update(_splash_art(revealed))
        if self._fade_step < _FADE_STEPS:
            return
        self._stop_timer(self._fade_timer)
        # Chain runs DOWN, once only: pause then tagline typing.
        self._wait_timer = self.set_timer(_TAG_WAIT, self._begin_typing)

    def _stop_timer(self, timer: Timer | None) -> None:
        """Stops a timer if it exists (guard against None)."""
        if timer is not None:
            timer.stop()

    def _begin_typing(self) -> None:
        if not self.is_mounted:
            return
        self._tagline = self.query_one("#splash-sub", Static)
        self._tagline.update("")
        self._typed = 0
        self._typing_timer = self.set_interval(_TAG_TICK, self._type_tick)

    @staticmethod
    def _tagline_text(body: str) -> Text:
        """Tagline in $muted, with no trailing cursor (stable width)."""
        spec = themes.spec()
        muted = Style(color=spec.muted)
        return Text(body, style=muted)

    def _type_tick(self) -> None:
        if not self.is_mounted:
            return
        assert self._tagline is not None
        self._typed += 1
        self._tagline.update(self._tagline_text(_TAGLINE[: self._typed]))
        if self._typed < len(_TAGLINE):
            return
        # Text fully typed: typing stops there, no cursor —
        # the chain is never restarted.
        self._stop_timer(self._typing_timer)

    def _stop_animation(self) -> None:
        """Stops every still-running animation timer (None guards)."""
        if self._fade_timer is not None:
            self._fade_timer.stop()
        if self._wait_timer is not None:
            self._wait_timer.stop()
        if self._typing_timer is not None:
            self._typing_timer.stop()
        if self._auto_timer is not None:
            self._auto_timer.stop()

    def _launch(self) -> None:
        if self._launched:
            return
        self._launched = True
        self._stop_animation()
        asyncio.create_task(self.app.begin())

    def action_skip(self) -> None:
        self._launch()