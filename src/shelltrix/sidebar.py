"""Sidebar droite : panneaux contextuels (Room / Session), style opencode.

Helpers de synthèse markup pour les deux panneaux latéraux, extraits de
`app.py`. Fonctions pures sur des objets room nio (aucun état Textual) :
  _sidebar_sync_parts, _sidebar_member_counts, _sidebar_power_label,
  _sidebar_room_markup, _sidebar_session_markup
Plus les primitives de décor partagées avec la sidebar gauche (titre de
section encadré, branches d'arbre) : `box_header`, `branch`, `tree_block`.
Dépend uniquement de `themes` et de `rich.markup.escape`.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.markup import escape

from . import themes

_SIDEBAR_TOPIC_CHARS = 40

# Largeur intérieure minimale d'un cadre : plancher de lisibilité, pour
# qu'un libellé reste centré même dans une sidebar très étroite (au-delà,
# le cadre s'élargit au lieu de tronquer le texte).
_BOX_MIN_INNER = 4


def box_header(title: str, width: int, *, color: str | None = None) -> list[str]:
    """Titre de section encadré (┌─┐ / │ Titre │ / └─┘) sur `width` colonnes.

    Le libellé est centré dans le cadre ; s'il ne tient pas, le cadre
    s'élargit plutôt que de tronquer le texte (les sidebars étant étroites
    en terminal réduit, la largeur demandée n'est qu'un plancher).

    Les trois lignes font EXACTEMENT `inner + 2` colonnes : `inner` pour
    l'intérieur, plus les deux coins. D'où `slack = inner - len(text)` —
    les deux barres verticales du milieu sont déjà les coins, il ne reste
    donc aucune colonne à leur réserver (sinon le cadre est « en escalier »,
    la ligne du titre 2 colonnes plus courte que les deux filets).
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
    """Marqueur de branche d'un élément de liste : `├─`, ou `└─` au dernier."""
    c = themes.muted()
    return f"[{c}]{'└─' if last else '├─'}[/{c}]"


def tree_block(rows: Sequence[str]) -> list[str]:
    """Préfixe chaque ligne d'un bloc par sa branche d'arbre.

    La dernière ligne ferme la branche (`└─`) : le bloc se lit alors comme
    une section encadrée, avec `box_header`, suivie de ses feuilles.
    """
    last = len(rows) - 1
    return [f"{branch(i == last)} {row}" for i, row in enumerate(rows)]


def _sidebar_sync_parts(sync_state: str) -> tuple[str, str]:
    """(libellé, couleur) de l'état de sync pour le panneau SESSION."""
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
    """(joined, invited) depuis le summary nio, sans erreur si absent."""
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
    """Rôle + niveau de pouvoir de l'utilisateur courant, ou None si inconnu."""
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
    # État vide : on laisse la timeline centrale porter le message
    # "Pick a room…" (écrit dans on_mount). Le répéter ici en l'absence de
    # salon doublait l'info sur deux zones à la fois (état mal nettoyé).
    # À long terme, un widget d'état vide dédié (un "empty state" partagé
    # par ces deux zones) serait plus propre qu'un message isolé dans la
    # timeline — à discuter avant tout refactor.
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