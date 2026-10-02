"""Right sidebar: contextual panels (Room / Session), opencode style.

Markup synthesis helpers for the two side panels, extracted from `app.py`.
Pure functions over nio room objects (no Textual state):
  _sidebar_sync_parts, _sidebar_member_counts, _sidebar_power_label,
  _sidebar_room_markup, _sidebar_session_markup
Plus the decoration primitives shared with the left sidebar (framed
section title, tree branches): `box_header`, `branch`, `tree_block`.
Depends on `themes` and `rich.markup.escape` only.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.markup import escape

from . import themes

_SIDEBAR_TOPIC_CHARS = 40

# Minimum inner width of a frame: readability floor, so that a label stays
# centered even in a very narrow sidebar (beyond that, the frame widens
# instead of truncating the text).
_BOX_MIN_INNER = 4


def box_header(title: str, width: int, *, color: str | None = None) -> list[str]:
    """Framed section title (┌─┐ / │ Title │ / └─┘) over `width` columns.

    The label is centered in the frame; if it does not fit, the frame
    widens rather than truncating the text (sidebars being narrow in a
    reduced terminal, the requested width is only a floor).

    The three lines take EXACTLY `inner + 2` columns: `inner` for the
    interior, plus the two corners. Hence `slack = inner - len(text)` —
    the two vertical bars in the middle are already the corners, so there
    is no column left to reserve for them (otherwise the frame is
    "staircase", the title line 2 columns shorter than both rules).
    """
    c = color or themes.border()
    inner = max(width - 2, len(title) + 2, _BOX_MIN_INNER)
    text = title if len(title) + 2 <= inner else title[: inner - 2]
    slack = inner - len(text)
    left = " " * (slack // 2)
    right = " " * (slack - slack // 2)
    return [
        f"[{c}]┌{'─' * inner}┐[/{c}]",
        f"[{c}]│[/{c}]{left}[bold]{escape(text)}" + "[/bold]" + f"{right}[{c}]│[/{c}]",
        f"[{c}]└{'─' * inner}┘[/{c}]",
    ]


def branch(last: bool = False) -> str:
    """Branch marker of a list item: `├─`, or `└─` on the last one."""
    c = themes.muted()
    return f"[{c}]{'└─' if last else '├─'}[/{c}]"


def tree_block(rows: Sequence[str]) -> list[str]:
    """Prefixes every line of a block with its tree branch.

    The last line closes the branch (`└─`): the block then reads like a
    framed section, with `box_header`, followed by its leaves.
    """
    last = len(rows) - 1
    return [f"{branch(i == last)} {row}" for i, row in enumerate(rows)]


def _sidebar_sync_parts(sync_state: str) -> tuple[str, str]:
    """(label, color) of the sync state for the SESSION panel."""
    if sync_state == "online":
        return "online", themes.success()
    if sync_state == "syncing":
        return "syncing…", themes.warning()
    if sync_state == "reconnecting":
        return "reconnecting…", themes.warning()
    if sync_state == "connecting":
        return "connecting", themes.muted()
    return "off-line", themes.error()


def _sidebar_member_counts(room: object) -> tuple[int | None, int | None]:
    """(joined, invited) from the nio summary, no error if absent."""
    summary = getattr(room, "summary", None)
    joined = getattr(summary, "joined_member_count", None)
    if joined is None:
        joined = getattr(summary, "m_joined_member_count", None)
    invited = getattr(summary, "invited_member_count", None)
    if invited is None:
        invited = getattr(summary, "m_invited_member_count", None)
    if joined is not None and joined < 0:
        joined = None
    if invited is not None and invited < 0:
        invited = None
    return joined, invited


def _sidebar_power_label(room: object, own_user_id: str) -> str | None:
    """Role + power level of the current user, or None if unknown."""
    power_levels = getattr(room, "power_levels", None)
    if power_levels is None:
        return None
    try:
        level = power_levels.get_user_level(own_user_id)
    except Exception:
        return None
    if level >= 100:
        role = "Admin"
    elif level >= 50:
        role = "Moderator"
    else:
        role = "User"
    return f"{role} ({level})"


def _sidebar_room_markup(room: object | None, own_user_id: str, width: int) -> str:
    m = themes.muted()
    t = themes.text()
    lines = box_header("ROOM", width)
    # Empty state: we let the central timeline carry the message
    # "Pick a room…" (written in on_mount). Repeating it here in the absence
    # of a room duplicated the info across two zones at once (state not
    # cleaned up). In the long run, a dedicated empty state widget (an
    # "empty state" shared by these two zones) would be cleaner than an
    # isolated message in the timeline — to discuss before any refactor.
    if room is None:
        return "\n".join(lines)
    room_id = getattr(room, "room_id", "?")
    name = getattr(room, "display_name", None) or room_id
    rows = [f"[bold][{t}]{escape(name)}[/{t}][/bold]"]
    alias = getattr(room, "canonical_alias", None)
    if alias:
        if len(alias) > 36:
            alias = alias[:35] + "…"
        rows.append(f"[{m}]{escape(alias)}[/{m}]")
    topic = getattr(room, "topic", None)
    if topic:
        if len(topic) > _SIDEBAR_TOPIC_CHARS:
            topic = topic[: _SIDEBAR_TOPIC_CHARS - 1] + "…"
        rows.append(f"[{m}]{escape(topic)}[/{m}]")
    joined, invited = _sidebar_member_counts(room)
    if joined is not None:
        count = f"{joined} joined"
        if invited:
            count += f" · {invited} invited"
        rows.append(f"[{m}]Members[/{m}]  [{m}]{count}[/{m}]")
    encrypted = getattr(room, "encrypted", None)
    badge = "E2EE enabled" if encrypted is True else "Unencrypted"
    rows.append(f"[{m}]Encryption[/{m}]  [{m}]{badge}[/{m}]")
    role = _sidebar_power_label(room, own_user_id)
    if role:
        rows.append(f"[{m}]Power[/{m}]  [{m}]{role}[/{m}]")
    lines.extend(tree_block(rows))
    return "\n".join(lines)


def _sidebar_session_markup(
    sync_state: str,
    next_batch: str | None,
    refresh_time: float | None,
    now: float,
    width: int,
) -> str:
    m = themes.muted()
    label, color = _sidebar_sync_parts(sync_state)
    lines = box_header("SESSION", width)
    rows = [f"● [bold][{color}]{label}[/{color}][/bold]"]
    if next_batch:
        if len(next_batch) > 28:
            next_batch = next_batch[:27] + "…"
        rows.append(f"[{m}]Token[/{m}]  [{m}]{escape(next_batch)}[/{m}]")
    if refresh_time is not None:
        age = max(0, int(now - refresh_time))
        rows.append(f"[{m}]Refresh[/{m}]  [{m}]{age}s ago[/{m}]")
    lines.extend(tree_block(rows))
    return "\n".join(lines)