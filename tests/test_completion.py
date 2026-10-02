"""Tests for @user / #room mention autocompletion.

Targets ChatScreen's pure helpers (mention parsing, room user list, room
list) without having to mount the full Textual app.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from shelltrix.screens.chat import ChatScreen


def make_screen(client: object | None = None) -> ChatScreen:
    """Build a ChatScreen with a mocked client, without mounting it."""
    if client is None:
        client = MagicMock()
        client.client.user_id = "@me:hs"
        client.rooms.return_value = {}
    return ChatScreen(client)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Parsing the mention trigger
# ----------------------------------------------------------------------
def test_find_mention_start_user() -> None:
    sc = make_screen()
    assert sc._find_mention_start("hello @ali", 10) == (6, "user", "ali")


def test_find_mention_start_room() -> None:
    sc = make_screen()
    assert sc._find_mention_start("look #mat", 9) == (5, "room", "mat")


def test_find_mention_start_no_mention() -> None:
    sc = make_screen()
    assert sc._find_mention_start("hello world", 11) is None


def test_mention_must_follow_space_or_start() -> None:
    """An @ in the middle of a word does not trigger completion."""
    sc = make_screen()
    # "a@b": the @ is not at the start of a word → None
    assert sc._find_mention_start("a@b c", 3) is None


def test_mention_with_space_is_not_completed() -> None:
    """A query containing a space cuts the mention (we are out)."""
    sc = make_screen()
    assert sc._find_mention_start("@ali bob", 6) is None


def test_mention_at_start() -> None:
    sc = make_screen()
    assert sc._find_mention_start("@alice", 6) == (0, "user", "alice")


def test_mix_hash_then_at_uses_nearest() -> None:
    sc = make_screen()
    # The @ is closer to the cursor than the # → the active mention is @
    assert sc._find_mention_start("#room and @alice", 16) == (10, "user", "alice")


# ----------------------------------------------------------------------
# List of a room's users (excludes the current user)
# ----------------------------------------------------------------------
def test_active_room_users_excludes_self() -> None:
    room = MagicMock()
    room.users = {"@me:hs": object(), "@alice:hs": object()}
    room.user_name.side_effect = lambda uid: {"@alice:hs": "Alice"}.get(uid)
    client = MagicMock()
    client.client.user_id = "@me:hs"
    client.rooms.return_value = {"!r:hs": room}
    sc = make_screen(client)
    sc.active_room_id = "!r:hs"
    users = sc._active_room_users()
    assert users == [("@alice:hs", "Alice")]


def test_active_room_users_no_active_room() -> None:
    sc = make_screen()
    assert sc._active_room_users() == []


# ----------------------------------------------------------------------
# List of rooms (preferred alias, otherwise id)
# ----------------------------------------------------------------------
def test_all_rooms_prefers_alias() -> None:
    room_a = MagicMock(canonical_alias="#alias:hs", display_name="Room A")
    room_b = MagicMock(canonical_alias=None, display_name="Room B")
    client = MagicMock()
    client.client.user_id = "@me:hs"
    client.rooms.return_value = {"!a:hs": room_a, "!b:hs": room_b}
    sc = make_screen(client)
    rooms = sc._all_rooms()
    # Sorted by display name: Room A before Room B
    assert rooms == [("#alias:hs", "Room A"), ("!b:hs", "Room B")]


# ----------------------------------------------------------------------
# Unread badge (capped at 99+)
# ----------------------------------------------------------------------
def test_unread_badge_small() -> None:
    assert ChatScreen._unread_badge(1) == "1"


def test_unread_badge_large_capped() -> None:
    assert ChatScreen._unread_badge(100) == "99+"
    assert ChatScreen._unread_badge(500) == "99+"


def test_unread_badge_boundary_99() -> None:
    assert ChatScreen._unread_badge(99) == "99"
