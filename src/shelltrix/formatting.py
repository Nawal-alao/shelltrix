"""Text formatting for the shelltrix interface: timeline, inline markdown,
time and fuzzy matching — pure functions, no Textual state.

Groups the helpers extracted from `app.py`:
  _sender_color, _inline_markdown, _format_time, _fuzzy_score
plus their private constants (color palette, markdown regexes, URL
regex). Depends on `themes` only.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from rich.markup import escape

from . import themes

# ---------------------------------------------------------------------------
# Conversational timeline
# ---------------------------------------------------------------------------

# A time separator (line + time) splits two messages from the same room
# when the silence between them exceeds this threshold.
TIME_GAP_SEPARATOR_MS = 5 * 60 * 1000

# Indentation of timeline lines: the body is offset so the author header
# (time + name) and the message text are not confused. This width is also
# that of the time gutter + 1 breathing space (see `_header_for` in the chat
# screen): both columns line up.
_TIMELINE_INDENT = " " * 8

# Length of the time separator rule.
_GAP_DASHES = 36


@dataclass
class TimelineEntry:
    """A timeline message, in structured form.

    Rendering (sender grouping, time separators) is computed at display
    time, not stored: this is what allows a grouped conversation instead of
    a repetitive log.
    """

    sender: str  # full user_id (@alice:hs)
    display_name: str  # resolved display name (for the block header)
    is_own: bool
    time_ms: int  # server timestamp in milliseconds
    body: str  # body already escaped + inline markdown
    event_id: str = ""  # server identifier (pagination/history dedup)
    msgtype: str = "m.text"  # message type (m.text, m.emote, m.image, …)
    has_mention: bool = False  # true if this message mentions us (@user)
    is_image: bool = False
    image_hint: str = ""  # e.g. filename for the placeholder
    timestamp: str = field(default="")  # "HH:MM" precomputed
    # Reply to another message (m.in_reply_to). `reply_to_name` is resolved
    # on receipt through the event_id → name index; empty if the quoted
    # message is unknown (outside history), in which case no quote line is
    # rendered rather than showing a guessed name.
    reply_to_event_id: str = ""
    reply_to_name: str = ""


@dataclass
class MessageBlock:
    """A timeline render block: ONE message and its dressing.

    The timeline is made of widgets (one per message) rather than a stream of
    lines: this is what makes hover, selection, copy and targeted reaction on
    a single message possible. `format_timeline_blocks` therefore produces
    blocks, which `format_timeline_entries` then flattens into lines.

    Attributes:
        entry: the rendered message (carries event_id, author, timestamp).
        lines: block Rich markup, header included unless `is_continuation`.
        is_continuation: header omitted (same sender, no silence).
        gap_before: silence > threshold before this block → time separator.
    """

    entry: TimelineEntry
    lines: list[str]
    is_continuation: bool = False
    gap_before: bool = False
    date_before: bool = False


@dataclass
class TimelineContext:
    """Grouping state in progress for a room.

    `last_sender`/`last_time_ms` reflect the last rendered entry: they
    decide whether the next entry continues the current block, opens a new
    block, or requires a time separator.
    """

    last_sender: str | None = None
    last_time_ms: int = 0
    # Date of the last rendered message (timestamp): used to insert a day
    # separator. Not used for grouping, which only depends on silence.
    last_day_ms: int = 0


def interval_time_gap(prev_ms: int, curr_ms: int) -> bool:
    """True if the silence between two messages exceeds the 5 min threshold."""
    return (curr_ms - prev_ms) >= TIME_GAP_SEPARATOR_MS


def body_mentions_user(body: str, user_id: str) -> bool:
    """True if the message body explicitly mentions `user_id`.

    Recognises both the full identifier (`@local:server`) and the plain
    localpart (`@local`). Pure function, tested.
    """
    if not body or not user_id:
        return False
    full = user_id.strip()
    if "@" not in full:
        return False
    localpart = full.split(":", 1)[0]
    for token in (full, localpart):
        if token in body:
            return True
    return False


def reply_quote_line(entry: TimelineEntry) -> str:
    """Citation line « ┌─ replying to X » (empty when there is no reply).

    The quote precedes the body: that order is what makes the context
    readable (Element, Discord). `┌─` visually indicates a line suspended
    ABOVE the message, which matches its position.

    The name is escaped: it comes from the network (arbitrary display_name).
    """
    if not entry.reply_to_name:
        return ""
    return f"[dim]┌─ replying to {escape(entry.reply_to_name)}[/dim]"


# ---------------------------------------------------------------------------
# Date separators
# ---------------------------------------------------------------------------


def local_date(time_ms: int) -> date:
    """LOCAL date of a Matrix timestamp (ms UTC).

    Spec timestamps are in UTC, but "Today" and "03:18" must follow the
    user's timezone: the conversion is made explicit rather than letting
    `fromtimestamp()` guess.
    """
    return datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc).astimezone().date()


def local_today() -> date:
    """Local date of today, same explicit conversion as `local_date`."""
    return datetime.now(tz=timezone.utc).astimezone().date()


# Weekday names in English, matching the rest of the UI. `strftime("%A")`
# would follow the process locale and could disagree with it.
_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def weekday_name(day: date) -> str:
    return _WEEKDAYS[day.weekday()]


def local_time_label(timestamp_ms: int) -> str:
    """Local time as `HH:MM` of a Matrix timestamp."""
    return datetime.fromtimestamp(
        timestamp_ms / 1000, tz=timezone.utc
    ).astimezone().strftime("%H:%M")


def day_label(time_ms: int, *, now_ms: int | None = None) -> str:
    """Readable day label: 'Today', 'Yesterday', or the date.

    The two nearby cases are spelled out (that is what you look
    for in a timeline); beyond that we fall back to the short date. The
    comparison is done on the LOCAL DATE, not UTC: "today" must follow the
    user's timezone, not the server's.
    """
    if not time_ms:
        return "Unknown date"
    day = local_date(time_ms)
    today = local_date(now_ms) if now_ms is not None else local_today()
    delta = (today - day).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if 1 < delta < 7:
        return weekday_name(day)
    return day.strftime("%d/%m/%Y")


def format_date_separator(time_ms: int, *, now_ms: int | None = None) -> str:
    """Day divider: `─── Today ─────`."""
    label = day_label(time_ms, now_ms=now_ms)
    tail = max(_GAP_DASHES - len(label) - 4, 3)
    return f"[dim]─── {label} {'─' * tail}[/dim]"


def needs_date_separator(prev_ms: int, curr_ms: int) -> bool:
    """True if the two timestamps fall on two different days."""
    if not prev_ms or not curr_ms:
        return False
    prev = local_date(prev_ms)
    curr = local_date(curr_ms)
    return prev != curr


# ---------------------------------------------------------------------------
# Replies (m.in_reply_to)
# ---------------------------------------------------------------------------


def reply_target_of(content: Mapping[str, object]) -> str:
    """Event_id of the message quoted by an `m.room.message`, or "" .

    Two forms coexist in the Matrix spec:
      - modern form: `m.relates_to.rel_type == "m.in_reply_to"`;
      - legacy form: `m.in_reply_to.event_id`.
    Both are read. `content` is the raw event's `content` dict — a pure
    function, testable without network or Textual.
    """
    relates = content.get("m.relates_to")
    if (
        isinstance(relates, Mapping)
        and relates.get("rel_type") == "m.in_reply_to"
    ):
        event_id = relates.get("event_id")
        if isinstance(event_id, str) and event_id:
            return event_id
    legacy = content.get("m.in_reply_to")
    if isinstance(legacy, Mapping):
        event_id = legacy.get("event_id")
        if isinstance(event_id, str) and event_id:
            return event_id
    return ""


def reply_fallback(body: str, author: str) -> str:
    """Fallback prefix the spec requires for a reply.

    A client that does not understand `m.in_reply_to` only shows the message
    body: without this prefix the reply loses all context. The spec mandates
    `<@user_id> original body`. `author` is the full identifier (@a:hs).
    """
    if not author:
        return body
    return f"<{author}> {body}"


def strip_reply_fallback(body: str, author: str) -> str:
    """Strips the fallback prefix we set ourselves when sending.

    Without this strip our own timeline would show the original message
    glued to the reply text (the server put it there for older clients).
    Only the EXACT prefix we built is removed, so a message legitimately
    starting with `<@alice:hs> …` is not cut of text that was not a
    fallback.
    """
    if not author:
        return body
    return body.removeprefix(f"<{author}> ")


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


def annotation_of(content: Mapping[str, object]) -> tuple[str, str]:
    """`(event_id, key)` of an `m.reaction` annotation, or `("", "")`.

    Replies and reactions share the same `m.relates_to` key: only `rel_type`
    separates them. Confusing the two would show a quote as a reaction,
    and above all pass a reaction off as an empty message — nio classifies
    an annotation as `RoomMessageText` with an empty body.
    """
    relates = content.get("m.relates_to")
    if not isinstance(relates, Mapping) or relates.get("rel_type") != "m.annotation":
        return "", ""
    target = relates.get("event_id")
    key = relates.get("key")
    if isinstance(target, str) and target and isinstance(key, str) and key:
        return target, key
    return "", ""


def reaction_counts(by_sender: Mapping[str, str]) -> dict[str, int]:
    """Counts reactions per emoji from a `sender -> key` index.

    We store the AUTHOR rather than an incremental counter because the spec
    allows only one reaction per user per message: changing emoji replaces
    the old one. A plain `count += 1` would accumulate both and show
    "👍 2" for someone who merely changed their mind.
    """
    counts: dict[str, int] = {}
    for key in by_sender.values():
        counts[key] = counts.get(key, 0) + 1
    return counts


def reaction_summary(counts: Mapping[str, int]) -> str:
    """Renders aggregated reactions: `👍 3  ❤️ 1`.

    The count is ALWAYS displayed, including for 1: a line
    `👍 3  ❤️` reads as if the second reaction had nobody, whereas
    `👍 3  ❤️ 1` gives the real number without having to guess.

    Deterministic order (sort by descending count then key) so that two
    consecutive renders do not jump from line to line. Keys are escaped:
    they come from the network.
    """
    if not counts:
        return ""
    parts = []
    for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        parts.append(f"[{themes.accent()}]{escape(key)} {n}[/{themes.accent()}]")
    return "  ".join(parts)


def format_timeline_blocks(
    entries: list[TimelineEntry],
    ctx: TimelineContext,
    *,
    header_for: Callable[[TimelineEntry], str],
) -> tuple[list[MessageBlock], TimelineContext]:
    """Converts entries into render blocks (one per message).

    Grouping rules, identical to `format_timeline_entries`:
      - first message of a fresh context → header + block line(s);
      - silence > threshold → `gap_before` (the time separator is rendered
        by the caller) then header + body;
      - consecutive same sender, no silence → plain indented body
        (`is_continuation`, no header);
      - different sender → new header.

    `header_for` is a callable(entry) → Rich markup of the header (with
    the time, the name and its color), provided by the caller since it
    depends on the theme. Time and name stay outside this module: here we
    only decide the STRUCTURE, the caller decides the rendering.

    Returns (blocks, final_context), the context applying to the rest of
    the list for incremental rendering consistent with full rendering.
    """
    blocks: list[MessageBlock] = []
    indent = _TIMELINE_INDENT
    for e in entries:
        gap = ctx.last_sender is not None and interval_time_gap(ctx.last_time_ms, e.time_ms)
        # The day separator is independent of silence: one isolated message
        # at 23:59 then one at 00:01 are only 2 minutes apart but
        # change day, so the date marker stays useful.
        day_change = needs_date_separator(ctx.last_day_ms, e.time_ms)
        continuation = ctx.last_sender is not None and not gap and e.sender == ctx.last_sender
        lines: list[str] = []
        if not continuation:
            lines.append(f"{indent}{header_for(e)}")
        quote = reply_quote_line(e)
        if quote:
            lines.append(f"{indent}{quote}")
        lines.append(f"{indent}{e.body}")
        blocks.append(
            MessageBlock(
                entry=e,
                lines=lines,
                is_continuation=continuation,
                gap_before=gap,
                date_before=day_change,
            )
        )
        ctx.last_sender = e.sender
        ctx.last_time_ms = e.time_ms
        ctx.last_day_ms = e.time_ms or ctx.last_day_ms
    return blocks, ctx


def format_timeline_entries(
    entries: list[TimelineEntry],
    ctx: TimelineContext,
    *,
    header_for: Callable[[TimelineEntry], str],
) -> tuple[list[str], TimelineContext]:
    """Flattens the blocks of `format_timeline_blocks` into Rich lines.

    Convenience view (tests, debugging, text rendering): a message of more
    than one line occupies several lines. On-screen rendering instead uses
    the blocks to have one widget per message.
    """
    blocks, ctx = format_timeline_blocks(entries, ctx, header_for=header_for)
    out: list[str] = []
    for b in blocks:
        if b.date_before:
            out.append("")
            out.append(format_date_separator(b.entry.time_ms))
        elif b.gap_before:
            ts = f"{b.entry.timestamp} " if b.entry.timestamp else ""
            out.append("")
            out.append(f"[dim]{ts}{'─' * _GAP_DASHES}[/dim]")
        out.extend(b.lines)
    return out, ctx


# ---------------------------------------------------------------------------
# Render helpers (timeline)
# ---------------------------------------------------------------------------

# Sender color palette: one stable color per identifier, derived by
# hashing, so each person always keeps their own.
# (Readable pastels on a dark background, in the opencode theme family.)
SENDER_COLORS = [
    "#a2d399", "#ffb4ab", "#ffd8a8", "#9ec3ff", "#c1ffb1",
    "#f0b8e0", "#baccb3", "#ffe58f", "#8cd8c8", "#d0d8ff",
]

_CODE_SPAN = re.compile(r"`([^`\n]+?)`")
_BOLD_SPAN = re.compile(r"\*\*([^*\n]+?)\*\*")
_ITALIC_SPAN = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_STRIKE_SPAN = re.compile(r"~~([^~\n]+?)~~")
_URL_RE = re.compile(r"https?://[^\s<>\"']+|www\.[^\s<>\"']+")


def _sender_color(sender: str) -> str:
    # usedforsecurity=False: the hash serves ONLY to derive a stable display
    # color per sender, it has no cryptographic role whatsoever.
    digest = hashlib.md5(sender.encode("utf-8"), usedforsecurity=False).hexdigest()
    return SENDER_COLORS[int(digest[:8], 16) % len(SENDER_COLORS)]


def _inline_markdown(body: str) -> str:
    """Converts the most common inline markdown to Rich markup.

    The body is escaped first (brackets stay literal) then markup is
    reinjected for code, bold, italic and strikethrough.
    """
    text = escape(body)
    code = themes.accent()
    # Order: code first (the most specific, delimited by rarely
    # ambiguous backticks), then strikethrough, bold and finally italic —
    # so that patterns do not overlap, each pass targets only the remaining
    # text (non-consecutive `**`, `~~`, `*` delimiters).
    text = _CODE_SPAN.sub(lambda m: f"[{code}]{m.group(1)}[/{code}]", text)
    text = _STRIKE_SPAN.sub(r"[strike]\1[/strike]", text)
    text = _BOLD_SPAN.sub(r"[bold]\1[/bold]", text)
    text = _ITALIC_SPAN.sub(r"[italic]\1[/italic]", text)
    return text


def highlight_mentions(markup: str, user_id: str) -> str:
    """Wraps mentions of `user_id` in a strong accent markup.

    Call AFTER `_inline_markdown`: the body is already escaped, so
    `@localpart` or `@local:server` mentions are findable as they are.
    False positives are avoided: a partial mention (`@bob2`) or a different
    identifier (`@bob:other`) is left untouched.
    """
    if not user_id or "@" not in user_id:
        return markup
    localpart = user_id.split(":", 1)[0]
    accent = themes.accent()
    full = re.escape(user_id)
    bare = re.escape(localpart)

    def _repl(m: re.Match) -> str:
        return f"[bold][{accent}]{m.group(0)}[/{accent}][/bold]"

    # `localpart` already contains the '@' (e.g. "@alice"); we search for it
    # as is, excluding the false positives `@bob2` / `@bob:other`.
    pattern = rf"({full})|({bare}(?![:\w]))"
    return re.sub(pattern, _repl, markup)


def _format_time(timestamp_ms: int | None) -> str:
    if not timestamp_ms:
        return ""
    return local_time_label(timestamp_ms)


def _fuzzy_score(query: str, candidate: str) -> float:
    """Fuzzy match score (ordered subsequence).

    Returns a score >= 0 if `query` appears as a subsequence in
    `candidate` (case-insensitive), -1 otherwise. Bonus for consecutive
    letters and an exact prefix, fzf-style.
    """
    q = query.casefold()
    c = candidate.casefold()
    if not q:
        return 0.0
    score = 0.0
    prev = -1
    for ch in q:
        pos = c.find(ch, prev + 1)
        if pos == -1:
            return -1.0
        score += 1.5 if pos == prev + 1 else 0.4
        prev = pos
    if c.startswith(q):
        score += 5.0
    return score