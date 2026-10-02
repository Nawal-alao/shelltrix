"""Tests pour le scrollback (historique serveur) côté ChatScreen.

Cible la conversion des événements d'historique nio en TimelineEntry prêtes à
être préfixées (`_entries_from_events`) : ordre chronologique, event_id, type
de message, détection de mention, et placeholder d'image.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from nio import RoomMessageImage, RoomMessageText

from shelltrix.formatting import reaction_counts
from shelltrix.screens.chat import ChatScreen


class _StubRoom:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    def user_name(self, sender: str) -> str | None:
        mapping = {"@alice:hs": "Alice", "@bob:hs": "Bob"}
        return mapping.get(sender)


class _StubInnerClient:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


class _StubClient:
    """Mini doublure de ShelltrixClient suffisante pour _entries_from_events."""

    def __init__(self) -> None:
        self.client = _StubInnerClient("@me:hs")
        self._rooms = {"!r:hs": _StubRoom("@me:hs")}

    def rooms(self) -> dict[str, _StubRoom]:
        return self._rooms


def make_screen() -> ChatScreen:
    return ChatScreen(_StubClient())  # type: ignore[arg-type]


def text_event(
    sender: str,
    body: str,
    ts: int,
    event_id: str,
    msgtype: str = "m.text",
) -> RoomMessageText:
    ev = MagicMock(spec=RoomMessageText)
    ev.sender = sender
    ev.body = body
    ev.server_timestamp = ts
    ev.event_id = event_id
    ev.msgtype = msgtype
    return ev


def image_event(sender: str, ts: int, event_id: str) -> RoomMessageImage:
    ev = MagicMock(spec=RoomMessageImage)
    ev.sender = sender
    ev.body = "photo.png"
    ev.server_timestamp = ts
    ev.event_id = event_id
    return ev


def test_entries_sorted_chronologically() -> None:
    screen = make_screen()
    later = text_event("@alice:hs", "newer", 3000, "ev3")
    earlier = text_event("@alice:hs", "older", 1000, "ev1")
    middle = text_event("@bob:hs", "middle", 2000, "ev2")
    entries = screen._entries_from_events("!r:hs", [later, earlier, middle])
    assert [e.time_ms for e in entries] == [1000, 2000, 3000]
    assert [e.event_id for e in entries] == ["ev1", "ev2", "ev3"]


def test_mention_detected_in_history() -> None:
    screen = make_screen()
    ev = text_event("@alice:hs", "hé @me regarde ça", 1000, "ev1")
    entries = screen._entries_from_events("!r:hs", [ev])
    assert entries[0].has_mention is True


def test_no_mention_in_history() -> None:
    screen = make_screen()
    ev = text_event("@alice:hs", "salut tout le monde", 1000, "ev1")
    entries = screen._entries_from_events("!r:hs", [ev])
    assert entries[0].has_mention is False


def test_image_entry_is_placeholder() -> None:
    screen = make_screen()
    ev = image_event("@alice:hs", 1000, "ev-img")
    entries = screen._entries_from_events("!r:hs", [ev])
    assert len(entries) == 1
    assert entries[0].is_image is True
    assert entries[0].msgtype == "m.image"
    assert "photo.png" in entries[0].image_hint


def test_own_message_displayed_as_vous() -> None:
    screen = make_screen()
    ev = text_event("@me:hs", "mon message", 1000, "ev1")
    entries = screen._entries_from_events("!r:hs", [ev])
    assert entries[0].is_own is True
    assert entries[0].display_name == "Vous"


def test_display_name_resolved_from_room() -> None:
    screen = make_screen()
    ev = text_event("@alice:hs", "bonjour", 1000, "ev1")
    entries = screen._entries_from_events("!r:hs", [ev])
    assert entries[0].display_name == "Alice"


def test_msgtype_preserved() -> None:
    screen = make_screen()
    ev = text_event("@bob:hs", "* action", 1000, "ev1", msgtype="m.emote")
    entries = screen._entries_from_events("!r:hs", [ev])
    assert entries[0].msgtype == "m.emote"


# ---------------------------------------------------------------------------
# Réactions dans l'historique
# ---------------------------------------------------------------------------


def reaction_event(sender: str, target: str, key: str, ts: int, event_id: str) -> RoomMessageText:
    """Annotation `m.reaction`.

    nio la décode en `RoomMessageText` au corps VIDE — c'est exactement ce qui
    rend le filtre indispensable : sans lui, chaque réaction de l'historique
    ajoute une ligne blanche dans la timeline.
    """
    ev = MagicMock(spec=RoomMessageText)
    ev.sender = sender
    ev.body = ""
    ev.server_timestamp = ts
    ev.event_id = event_id
    ev.msgtype = "m.text"
    ev.source = {
        "content": {
            "msgtype": "m.text",
            "body": "",
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": target,
                "key": key,
            },
        }
    }
    return ev


def test_annotation_never_becomes_a_blank_message() -> None:
    screen = make_screen()
    chunk = [
        text_event("@alice:hs", "premier", 1000, "ev1"),
        reaction_event("@bob:hs", "ev1", "\U0001f44d", 1100, "ev-r1"),
        text_event("@bob:hs", "deuxieme", 1200, "ev2"),
    ]
    entries = screen._entries_from_events("!r:hs", chunk)
    assert [e.event_id for e in entries] == ["ev1", "ev2"]
    assert all(e.body.strip() for e in entries)


def test_history_reactions_are_harvested() -> None:
    screen = make_screen()
    chunk = [
        text_event("@alice:hs", "premier", 1000, "ev1"),
        reaction_event("@bob:hs", "ev1", "\U0001f44d", 1100, "ev-r1"),
        reaction_event("@ann:hs", "ev1", "\U0001f44d", 1200, "ev-r2"),
        reaction_event("@bob:hs", "ev2", "❤️", 1300, "ev-r3"),
    ]
    seen = screen._harvest_reactions("!r:hs", chunk)
    assert seen == 3
    assert screen._reactions[("!r:hs", "ev1")] == {
        "@bob:hs": "\U0001f44d",
        "@ann:hs": "\U0001f44d",
    }
    assert screen._reactions[("!r:hs", "ev2")] == {"@bob:hs": "❤️"}


def test_harvest_is_idempotent_for_a_repeated_event() -> None:
    """La même annotation vue deux fois (sync + pagination) ne compte qu'une fois."""
    screen = make_screen()
    rx = reaction_event("@bob:hs", "ev1", "\U0001f44d", 1100, "ev-r1")
    screen._harvest_reactions("!r:hs", [rx])
    screen._harvest_reactions("!r:hs", [rx])
    assert screen._reactions[("!r:hs", "ev1")] == {"@bob:hs": "\U0001f44d"}


def test_changed_reaction_replaces_the_previous_key() -> None:
    """Changer d'emoji ne doit pas laisser l'ancien compteur derrière."""
    screen = make_screen()
    screen._record_reaction("!r:hs", "ev1", "\U0001f44d", "@bob:hs")
    screen._record_reaction("!r:hs", "ev1", "❤️", "@bob:hs")
    bucket = screen._reactions[("!r:hs", "ev1")]
    assert bucket == {"@bob:hs": "❤️"}
    assert reaction_counts(bucket) == {"❤️": 1}


def test_harvest_ignores_events_without_a_target() -> None:
    screen = make_screen()
    assert screen._harvest_reactions("!r:hs", [text_event("@a:hs", "hi", 1, "ev1")]) == 0
