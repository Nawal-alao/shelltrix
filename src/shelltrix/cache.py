"""Cache SQLite local des messages de timeline.

Persiste les `TimelineEntry` par salon (dédupliquées par `event_id`) afin de :
  - ne pas perdre l'historique reçu via sync/scrollback à chaque fermeture ;
  - afficher instantanément un salon déjà vu (offline-ish) avant même que le
    scrollback serveur ne revienne.

Le cache est scopé par `user_id` (prêt pour le multi-comptes) et stocké dans
`CONFIG_DIR/cache/`. Durcissement :
  - répertoire en 0700 et base en 0600 (contenus privés) ;
  - plafond de stockage (`MAX_CACHE_BYTES`) : au-delà, on purge les plus
    vieux messages (garde `CACHE_KEEP_PER_ROOM` entrées max par salon) pour
    ne jamais laisser le cache grossir sans limite.

SÉCURITÉ : les corps de messages sont stockés EN CLAIR (recherche SQL
nécessaire). Le cache dépend donc du périmètre du compte local + permissions ;
pour une protection au repos du support complet, il faut un chiffrement de
disque (LUKS/FileVault). Le store E2EE, lui, est chiffré (Fernet).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import CONFIG_DIR
from .formatting import TimelineEntry

# Garde-fous : plafond du fichier cache et entrées conservées par salon.
# 128 Mo sur disque, 4000 messages par salon (défaut, surchargeable via les
# constantes — il n'y a pas encore de clé de config dédiée).
MAX_CACHE_BYTES = 128 * 1024 * 1024
CACHE_KEEP_PER_ROOM = 4000
# Fréquence du contrôle de taille : tous les N inserts (un stat() par message
# serait trop coûteux).
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

# Constantes SQL : les colonnes proviennent exclusivement de la constante
# interne _EVENT_COLUMNS (aucune donnée utilisateur) et TOUTES les valeurs
# passent par des placeholders "?" — pas de interpolation possible.
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
    """Cache SQLite des messages d'un seul compte."""

    def __init__(self, user_id: str, cache_dir: Path | None = None) -> None:
        # Le user_id (@alice:hs) contient des caractères non-valides pour un nom
        # de fichier ; on les neutralise pour un nom stable et sûr.
        safe = user_id.replace("@", "at_").replace(":", "_").replace("/", "_")
        self.user_id = user_id
        if cache_dir is None:
            cache_dir = _cache_dir()
        self.path = cache_dir / f"{safe}.db"
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        # Permissions durcies : base en 0600, mise en vigueur même sur un
        # fichier pré-existant (l'utilisateur peut avoir créé la base avec
        # un umask permissif).
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
        """Ajoute les colonnes manquantes à une base préexistante.

        `CREATE TABLE IF NOT EXISTS` ne modifie pas une table déjà créée sur
        disque : sans cette étape, une base issue d'une version antérieure
        n'aurait pas les colonnes `reply_to_*` et tous les SELECT échoueraient.
        On compare donc `PRAGMA table_info` à la liste attendue et on n'ajoute
        que le manque, avec `DEFAULT ''` pour rester compatible NOT NULL.
        """
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(messages)")}
        if not have:
            return  # table absente : le CREATE ci-dessus vient de la créer
        for column in _EVENT_COLUMNS:
            if column in have:
                continue
            # `column` vient de la constante interne _EVENT_COLUMNS, jamais
            # d'une donnée utilisateur ; les valeurs restent paramétrées.
            self._conn.execute(
                f"ALTER TABLE messages ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"  # nosec B608
            )

    def _prune_if_oversized(self) -> None:
        """Purge les plus vieux messages si le cache dépasse MAX_CACHE_BYTES.

        Conserve au plus `CACHE_KEEP_PER_ROOM` entrées par (user, room), en
        gardant les plus récentes. Évite une base qui grossit sans limite
        tout en préservant la recherche locale sur l'historique récent.
        """
        try:
            if self.path.stat().st_size <= MAX_CACHE_BYTES:
                return
        except OSError:
            return
        # Classe les entrées par (user, room) par time_ms DESC puis ne garde
        # que les CACHE_KEEP_PER_ROOM plus récentes (window function SQLite).
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
        """Insère ou remplace les entrées non vides par `event_id`."""
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
        # Plafond de stockage : contrôlé périodiquement pour éviter un stat()
        # syscall à chaque message.
        self._insert_count += len(rows)
        if self._insert_count >= _PRUNE_EVERY:
            self._insert_count = 0
            self._prune_if_oversized()

    def load_entries(self, room_id: str) -> list[TimelineEntry]:
        """Charge les entrées d'un salon, triées par timestamp croissant."""
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
        """Recherche insensible à la casse dans le corps des messages.

        `room_id` restreint la recherche à un salon (None = tous). Résultats
        triés du plus récent au plus ancien, plafonnés à `limit`.
        """
        return [e for _, e in self.search_with_room(query, room_id, limit)]

    def search_with_room(
        self,
        query: str,
        room_id: str | None = None,
        limit: int = 200,
    ) -> list[tuple[str, TimelineEntry]]:
        """Comme `search_messages`, mais renvoie aussi le salon de chaque hit.

        Chaque élément est `(room_id, entry)`, ce qui permet d'afficher le nom
        du salon et de naviguer vers le message (recherche multi-salons).
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
        """Efface les messages mis en cache d'un salon (ex. action clear)."""
        self._conn.execute(
            "DELETE FROM messages WHERE user_id=? AND room_id=?",
            (self.user_id, room_id),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
