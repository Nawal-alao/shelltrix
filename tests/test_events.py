"""Tests for the library-independent event contract (`shelltrix.events`).

Two jobs here:

1. The boundary (`matrix_client._to_room` and friends) must normalize exactly
   what matrix-nio resolved, because the UI now depends on that output instead
   of on nio. A drift would silently change every displayed name.
2. Nothing above the transport may import nio anymore. That invariant is what
   makes swapping matrix-nio for the Rust core a contained change, so it is
   enforced by a test rather than left to memory.
"""

from __future__ import annotations

import ast
import pathlib

from nio import MatrixRoom, MatrixUser
from nio import RoomMessageImage

from shelltrix.events import Room
from shelltrix.matrix_client import _image_url, _to_room


def _image_event(content: dict, url: str = "") -> RoomMessageImage:
    """A nio image event; it reads a few fields back out of `source`."""
    return RoomMessageImage(
        source={
            "event_id": "$e1",
            "sender": "@alice:hs",
            "origin_server_ts": 1700,
            "content": {"body": "p.png", **content},
        },
        url=url,
        body="p.png",
    )

PKG = pathlib.Path(__file__).resolve().parent.parent / "src" / "shelltrix"


def _room_with(*members: tuple[str, str | None]) -> MatrixRoom:
    """A nio room with the given (user_id, display_name) members."""
    room = MatrixRoom("!r:hs", "@me:hs")
    for user_id, display_name in members:
        user = MatrixUser(user_id, display_name)
        room.users[user_id] = user
        room.names[user.name].append(user_id)
    return room


# ---------------------------------------------------------------------------
# Parity with matrix-nio
# ---------------------------------------------------------------------------


def test_user_name_matches_nio_when_unique() -> None:
    normalized = _to_room(_room_with(("@alice:hs", "Alice")))
    assert normalized.user_name("@alice:hs") == "Alice"


def test_user_name_matches_nio_when_two_members_share_a_name() -> None:
    """nio disambiguates homonyms; the UI depends on it to stay readable."""
    normalized = _to_room(_room_with(("@alice:hs", "Alice"), ("@alice2:hs", "Alice")))
    assert normalized.user_name("@alice:hs") == "Alice (@alice:hs)"
    assert normalized.user_name("@alice2:hs") == "Alice (@alice2:hs)"


def test_user_name_is_none_for_a_non_member() -> None:
    normalized = _to_room(_room_with(("@alice:hs", "Alice")))
    assert normalized.user_name("@stranger:hs") is None


def test_display_name_matches_nio() -> None:
    members = (("@alice:hs", "Alice"), ("@bob:hs", "Bob"))
    assert _to_room(_room_with(*members)).display_name == _room_with(*members).display_name


def test_a_room_with_no_display_name_still_renders_something() -> None:
    """nio resolves an empty room to "Empty Room"; we keep whatever it says.

    The only requirement at this layer is that `display_name` is never blank,
    because the sidebar renders it directly.
    """
    normalized = _to_room(MatrixRoom("!r:hs", "@me:hs"))
    assert normalized.display_name


def test_a_member_without_a_display_name_falls_back_to_their_id() -> None:
    normalized = _to_room(_room_with(("@alice:hs", None)))
    assert normalized.user_name("@alice:hs") == "@alice:hs"


# ---------------------------------------------------------------------------
# Image URL resolution
# ---------------------------------------------------------------------------


def test_image_url_prefers_the_v3_field() -> None:
    event = _image_event({"url": "mxc://hs/new"}, url="mxc://hs/new")
    assert _image_url(event) == "mxc://hs/new"


def test_image_url_falls_back_to_the_legacy_file_field() -> None:
    """Old servers and encrypted uploads nest the URL under `file`."""
    event = _image_event({"file": {"url": "mxc://hs/legacy"}})
    assert _image_url(event) == "mxc://hs/legacy"


def test_image_url_is_empty_when_the_event_carries_none() -> None:
    event = _image_event({})
    assert _image_url(event) == ""


# ---------------------------------------------------------------------------
# The architectural invariant
# ---------------------------------------------------------------------------

# The transport layer is allowed to know about nio; nothing above it is.
TRANSPORT_MODULES = {"matrix_client.py"}


def _imports_nio(path: pathlib.Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "nio" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "nio":
                return True
    return False


def test_no_module_above_the_transport_imports_nio() -> None:
    """The UI must not depend on matrix-nio.

    shelltrix-events are the contract between the UI and whatever transport
    speaks Matrix. As long as this holds, replacing matrix-nio with the Rust
    core touches `matrix_client.py` only — which is the entire point of
    routing the migration through a seam.
    """
    offenders = [
        str(path.relative_to(PKG))
        for path in sorted(PKG.rglob("*.py"))
        if path.name not in TRANSPORT_MODULES and _imports_nio(path)
    ]
    assert offenders == [], (
        "these modules import matrix-nio directly, which breaks the transport "
        f"seam: {offenders}"
    )


def test_the_transport_is_the_only_module_allowed_to_know_nio() -> None:
    """Sanity check on the invariant above: the allowance is not vacuous."""
    assert _imports_nio(PKG / "matrix_client.py") is True


def test_room_exposes_only_what_the_ui_uses() -> None:
    """Guards the dataclass against growing back towards nio's surface.

    `Room` replaced `nio.MatrixRoom`, which the UI used for exactly four
    things. Anything else added here should be a decision, not a convenience.
    """
    room = Room(room_id="!r:hs", display_name="Room")
    assert (room.room_id, room.display_name, room.name, room.user_name("@a:hs")) == (
        "!r:hs",
        "Room",
        None,
        None,
    )