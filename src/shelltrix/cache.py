"""Local SQLite cache of timeline messages.

Persists the `TimelineEntry` per room (deduplicated by `event_id`) so that:
  - history received via sync/scrollback is not lost on every shutdown;
  - an already seen room shows up instantly (offline-ish) even before the
    server scrollback comes back.

The cache is scoped by `user_id` (ready for multi-account) and stored in
`CONFIG_DIR/cache/`. Hardening:
  - directory at 0700 and database at 0600 (private contents);
  - storage cap (`MAX_CACHE_BYTES`): beyond it, we purge the oldest
    messages (keeping `CACHE_KEEP_PER_ROOM` entries max per room) so the
    cache never grows without bound.

SECURITY: message bodies are stored IN PLAINTEXT (SQL search requires it).
The cache therefore depends on the local account perimeter + permissions;
for at-rest protection of the whole medium, disk encryption is required
(LUKS/FileVault). The E2EE store, on the other hand, is encrypted (Fernet).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import CONFIG_DIR
from .formatting import TimelineEntry

# Guardrails: cache file size cap and entries kept per room.
# 128 MB on disk, 4000 messages per room (default, overridable through the
# constants — there is no dedicated config key yet).
MAX_CACHE_BYTES = 128 * 1024 * 1024
CACHE_KEEP_PER_ROOM = 4000
# Size check frequency: every N inserts (a stat() per message would be
# too expensive).
_PRUNE_EVERY = 64

_EVENT_COLUMNS = (
    "room_id",
    "event_id",
    "sender",
    "display_name",
    "is_own",
    "time_ms",
    "body",
    "msgtype",
    "has_mention",
    "is_image",
    "image_hint",
    "timestamp",
    "reply_to_event_id",
    "reply_to_name",
)

# SQL constants: the columns come exclusively from the internal constant
# _EVENT_COLUMNS (no user data) and ALL values go through "?" placeholders
# — no interpolation possible.
_ALWAYS_COLUMNS = ", ".join(_EVENT_COLUMNS)
_ALWAYS_PLACEHOLDERS = ", ".join(["?"] * (len(_EVENT_COLUMNS) + 1))
_UPSERT_SQL = (
    "INSERT OR REPLACE INTO messages ("
    + "user_id, "
    + _ALWAYS_COLUMNS
    + ") VALUES ("
    + _ALWAYS_PLACEHOLDERS
    + ")"
)
_ROOM_SELECT_SQL = (
    "SELECT " + _ALWAYS_COLUMNS  # nosec B608
    + " FROM messages WHERE user_id=? AND room_id=? ORDER BY time_ms"
)
_SEARCH_ROOM_SQL = (
    "SELECT room_id, " + _ALWAYS_COLUMNS  # nosec B608
    + " FROM messages WHERE user_id=? AND LOWER(body) LIKE LOWER(?)"
    + " AND room_id=? ORDER BY time_ms DESC LIMIT ?"
)
_SEARCH_ALL_SQL = (
    "SELECT room_id, " + _ALWAYS_COLUMNS  # nosec B608
    + " FROM messages WHERE user_id=? AND LOWER(body) LIKE LOWER(?)"
    + " ORDER BY time_ms DESC LIMIT ?"
)


def _cache_dir() -> Path:
    d = CONFIG_DIR / "cache"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


class MessageCache:
    """SQLite cache of a single account's messages."""

    def __init__(self, user_id: str, cache_dir: Path | None = None) -> None:
        # The user_id (@alice:hs) contains characters invalid for a file
        # name; we neutralize them for a stable, safe name.
        safe = user_id.replace("@", "at_").replace(":", "_").replace("/", "_")
        self.user_id = user_id
        if cache_dir is None:
            cache_dir = _cache_dir()
        self.path = cache_dir / f"{safe}.db"
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        # Hardened permissions: database at 0600, enforced even on a
        # pre-existing file (the user may have created the database with
        # a permissive umask).
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self._insert_count = 0
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                user_id TEXT NOT NULL,
                room_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                display_name TEXT NOT NULL,
                is_own INTEGER NOT NULL,
                time_ms INTEGER NOT NULL,
                body TEXT NOT NULL,
                msgtype TEXT NOT NULL,
                has_mention INTEGER NOT NULL,
                is_image INTEGER NOT NULL,
                image_hint TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                reply_to_event_id TEXT NOT NULL DEFAULT '',
                reply_to_name TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (user_id, room_id, event_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_room
                ON messages (user_id, room_id, time_ms);
            """
        )
        self._migrate_schema()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        """Adds the missing columns to a pre-existing database.

        `CREATE TABLE IF NOT EXISTS` does not modify a table already created
        on disk: without this step, a database from an earlier version would
        lack the `reply_to_*` columns and every SELECT would fail. We
        therefore compare `PRAGMA table_info` with the expected list and only
        add what is missing, with `DEFAULT ''` to stay NOT NULL compatible.
        """
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(messages)")}
        if not have:
            return  # table missing: the CREATE above just created it
        for column in _EVENT_COLUMNS:
            if column in have:
                continue
            # `column` comes from the internal constant _EVENT_COLUMNS, never
            # from user data; values stay parameterized.
            self._conn.execute(
                f"ALTER TABLE messages ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"  # nosec B608
            )

    def _prune_if_oversized(self) -> None:
        """Purges the oldest messages if the cache exceeds MAX_CACHE_BYTES.

        Keeps at most `CACHE_KEEP_PER_ROOM` entries per (user, room), by
        keeping the most recent ones. Avoids a database growing without bound
        while preserving local search over recent history.
        """
        try:
            if self.path.stat().st_size <= MAX_CACHE_BYTES:
                return
        except OSError:
            return
        # Sorts entries per (user, room) by time_ms DESC then keeps
        # only the CACHE_KEEP_PER_ROOM most recent (SQLite window function).
        self._conn.execute(
            "DELETE FROM messages WHERE rowid IN ("
            "  SELECT rowid FROM ("
            "    SELECT rowid, ROW_NUMBER() OVER ("
            "      PARTITION BY user_id, room_id ORDER BY time_ms DESC"
            "    ) AS rn FROM messages"
            "  ) WHERE user_id=? AND rn > ?"
            ")",
            (self.user_id, CACHE_KEEP_PER_ROOM),
        )
        self._conn.commit()

    def upsert_entries(self, room_id: str, entries: list[TimelineEntry]) -> None:
        """Inserts or replaces the non-empty entries by `event_id`."""
        if not entries:
            return
        rows = []
        for e in entries:
            if not e.event_id:
                continue
            rows.append(
                (
                    self.user_id,
                    room_id,
                    e.event_id,
                    e.sender,
                    e.display_name,
                    int(e.is_own),
                    e.time_ms,
                    e.body,
                    e.msgtype,
                    int(e.has_mention),
                    int(e.is_image),
                    e.image_hint,
                    e.timestamp,
                    e.reply_to_event_id,
                    e.reply_to_name,
                )
            )
        self._conn.executemany(_UPSERT_SQL, rows)
        self._conn.commit()
        # Storage cap: checked periodically to avoid a stat() syscall on
        # every message.
        self._insert_count += len(rows)
        if self._insert_count >= _PRUNE_EVERY:
            self._insert_count = 0
            self._prune_if_oversized()

    def load_entries(self, room_id: str) -> list[TimelineEntry]:
        """Loads a room's entries, sorted by ascending timestamp."""
        rows = self._conn.execute(
            _ROOM_SELECT_SQL,
            (self.user_id, room_id),
        ).fetchall()
        return [self._entry_from_row(r) for r in rows]

    def search_messages(
        self,
        query: str,
        room_id: str | None = None,
        limit: int = 200,
    ) -> list[TimelineEntry]:
        """Case-insensitive search in the message bodies.

        `room_id` restricts the search to one room (None = all). Results
        sorted from newest to oldest, capped at `limit`.
        """
        return [e for _, e in self.search_with_room(query, room_id, limit)]

    def search_with_room(
        self,
        query: str,
        room_id: str | None = None,
        limit: int = 200,
    ) -> list[tuple[str, TimelineEntry]]:
        """Like `search_messages`, but also returns the room of each hit.

        Each element is `(room_id, entry)`, which lets us display the room
        name and navigate to the message (multi-room search).
        """
        q = query.strip()
        if not q:
            return []
        params: list[str] = [self.user_id, f"%{q}%"]
        if room_id:
            sql = _SEARCH_ROOM_SQL
            params.append(room_id)
        else:
            sql = _SEARCH_ALL_SQL
        params.append(str(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [(r["room_id"], self._entry_from_row(r)) for r in rows]

    @staticmethod
    def _entry_from_row(r: sqlite3.Row) -> TimelineEntry:
        return TimelineEntry(
            sender=r["sender"],
            display_name=r["display_name"],
            is_own=bool(r["is_own"]),
            time_ms=r["time_ms"],
            body=r["body"],
            event_id=r["event_id"],
            msgtype=r["msgtype"],
            has_mention=bool(r["has_mention"]),
            is_image=bool(r["is_image"]),
            image_hint=r["image_hint"],
            timestamp=r["timestamp"],
            reply_to_event_id=r["reply_to_event_id"],
            reply_to_name=r["reply_to_name"],
        )

    def clear_room(self, room_id: str) -> None:
        """Erases a room's cached messages (e.g. clear action)."""
        self._conn.execute(
            "DELETE FROM messages WHERE user_id=? AND room_id=?",
            (self.user_id, room_id),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
