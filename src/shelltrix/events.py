"""The events shelltrix speaks, independent of any Matrix library.

The UI used to receive matrix-nio objects directly: `MatrixRoom`,
`RoomMessageText`, `RoomMessageImage`. That made the Textual layer depend on
nio, so swapping the transport for `matrix-sdk` would have meant rewriting
the UI. These dataclasses are the contract instead.

The rule is that a transport RESOLVES everything and the UI CONSUMES values.
Concretely, `Room.user_names` already holds disambiguated display names
("Alice" — or "Alice (@bob:hs)" when two members share a name), because
reproducing matrix-nio's naming rules in Python would be copying logic that
every transport would have to keep in sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class Room:
    """A room, as the UI needs to see it.

    `display_name` is never empty: transports resolve the fallback chain
    (room name, then alias, then the member-list "Alice and Bob" form)
    before constructing this.
    """

    room_id: str
    display_name: str
    name: str | None = None
    user_names: Mapping[str, str] = field(default_factory=dict)

    def user_name(self, user_id: str) -> str | None:
        """Display name of a member, or None if they are not in `user_names`.

        Returning None for unknown members is what the UI expects: it falls
        back to the raw user id, which is the best guess available.
        """
        return self.user_names.get(user_id)


@dataclass(frozen=True)
class MessageEvent:
    """An incoming text message.

    `source` is the raw event JSON. We keep it because the UI reads relations
    from it (`m.relates_to` for replies and annotations) and because nio
    decodes a reaction as a message with an empty body — filtering those out
    needs the original content, not the decoded fields.
    """

    room_id: str
    sender: str
    body: str
    event_id: str | None = None
    server_timestamp: int | None = None
    msgtype: str = "m.text"
    source: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageEvent:
    """An incoming image message.

    `url` is already resolved from whichever shape the homeserver used
    (`url` for v3 media, `file.url` for legacy), because the UI should not
    have to know that media uploads were reworked in Matrix v3.
    """

    room_id: str
    sender: str
    body: str
    event_id: str | None = None
    server_timestamp: int | None = None
    url: str = ""
    source: Mapping[str, object] = field(default_factory=dict)

@dataclass(frozen=True)
class MessagePage:
    """A page of room history.

    `start` and `end` are the pagination tokens; `end` is what shelltrix
    passes back as `start` to walk further back in time.
    """

    events: list[MessageEvent | ImageEvent]
    start: str | None = None
    end: str | None = None
