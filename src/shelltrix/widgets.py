"""Widgets UI maison réutilisables, extraits de `app.py`.

`_SendButton` : bouton d'envoi « → » plat (un Static cliquable).
`MessageView` : un message de la timeline dans un widget dédié — c'est ce
qui rend possible le survol, le collapse d'un message long, les réactions et
le reply ciblé (un `RichLog`, en écriture seule, ne le permet pas).

Aucun import vers `app`, `screens` ou `dialogs` — ces widgets opèrent sur
leur écran hôte par introspection.
"""

from __future__ import annotations

from rich.padding import Padding
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

from .formatting import MessageBlock

# Nombre de lignes de corps visibles quand un message est replié. Au-delà,
# un message long (logs,aits de code, listes) pousse les autres hors de
# l'écran : le repli garde la conversation lisible sur les rooms actives.
COLLAPSED_LINES = 5

# Largeur de la gouttière réservée à l'heure et au nom d'auteur. Doit rester
# alignée sur la constante équivalente du rendu des blocs.
_GUTTER_COLS = 8


class _SendButton(Static):
    """Bouton d'envoi « → » plat : un Static cliquable.

    Un vrai Button Textual impose `line-pad >= 1` et une hauteur minimale de
    3 lignes, ce qui rend son libellé illisible dans la barre de saisie
    (hauteur 1). On remplace donc par un Static dont le clic relance le même
    chemin d'envoi que la touche Entrée.
    """

    def on_click(self, event: events.Click) -> None:
        event.stop()
        screen = self.screen
        if screen.active_room_id is None:
            return
        self.run_worker(screen._dispatch_compose(screen.query_one("#composer", Input)))


def _gutter(markup: str) -> Padding:
    """Habille un corps de message dans la gouttière de la timeline.

    L'indentation est appliquée PAR LE RENDU et non en insérant des espaces dans
    la chaîne : c'est la seule façon de la conserver sur les lignes de
    continuation après habillage à la largeur du terminal (des espaces en tête
    de chaîne ne servent qu'à la première ligne).
    """
    return Padding(Text.from_markup(markup), (0, 0, 0, _GUTTER_COLS))


class MessageView(Vertical):
    """Un message de la timeline (en-tête, citation, corps, réactions).

    Le widget porte les métadonnées du message (`event_id`, `sender`,
    `is_own`) : les interactions futures (répondre, réagir, copier) se
    ciblent par `event_id` et n'ont plus à re-parcourir le log.

    Le repli s'applique au CORPS seul — l'en-tête (heure + auteur) et les
    réactions restent toujours visibles, sinon on perdrait l'information de
    QUI a écrit quoi. Le corps est tronqué par `max-height` (donc sur de
    vraies lignes rendues, pas sur une estimation de caractères) et un
    « … voir plus » cliquable est ajouté en dessous.
    """

    DEFAULT_CSS = """
    MessageView {
        height: auto;
        width: 1fr;
    }
    MessageView > .msg-head {
        height: auto;
        width: 1fr;
    }
    MessageView > .msg-body {
        height: auto;
        width: 1fr;
    }
    MessageView > .msg-reactions {
        height: auto;
        width: 1fr;
        display: none;
    }
    MessageView > .msg-more {
        height: auto;
        width: 1fr;
        display: none;
    }
    MessageView.-collapsed > .msg-body {
        max-height: $COLLAPSED_LINES;
    }
    MessageView.-collapsed > .msg-more {
        display: block;
    }
    """.replace("$COLLAPSED_LINES", str(COLLAPSED_LINES))

    def __init__(
        self,
        block: MessageBlock,
        *,
        body: str = "",
        reactions: str = "",
        collapsed: bool = False,
    ) -> None:
        super().__init__(classes="timeline-message")
        self.block = block
        self.event_id = block.entry.event_id
        self.sender = block.entry.sender
        self.is_own = block.entry.is_own
        self.time_ms = block.entry.time_ms
        self._body_markup = body or block.entry.body
        self._reactions_markup = reactions
        self._reactions_widget: Static | None = None
        self._collapsed = collapsed

    def compose(self) -> ComposeResult:
        # Tout ce qui précède le corps (en-tête d'auteur, ligne de citation)
        # va dans un seul widget : rien de tout cela n'est jamais tronqué.
        head = "\n".join(self.block.lines[:-1])
        reactions = Static(self._reactions_markup, classes="msg-reactions")
        reactions.display = bool(self._reactions_markup)
        self._reactions_widget = reactions
        self.set_class(self._collapsed, "-collapsed")
        yield Static(head, classes="msg-head")
        yield Static(_gutter(self._body_markup), classes="msg-body")
        yield reactions
        yield Static("… see more", classes="msg-more")

    def _set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self.set_class(collapsed, "-collapsed")

    def on_click(self, event: events.Click) -> None:
        """Bascule le repli du corps quand on clique « … voir plus ».

        On teste `event.widget` (le widget réellement sous la souris) et non
        `event.chain` : cette dernière ne contient que des décalages, pas les
        widgets traversés.
        """
        target = event.widget
        if target is not None and "msg-more" in target.classes:
            event.stop()
            self._set_collapsed(False)

    def add_reaction(self, markup: str) -> None:
        """Affiche une ligne de réactions sous le corps.

        Le markup est mémorisé même si le widget n'existe pas encore : une
        réaction peut arriver entre le montage du message et son compose. Dans
        ce cas compose() lira la valeur à jour, donc rien n'est perdu.
        """
        self._reactions_markup = markup
        if self._reactions_widget is None:
            return
        self._reactions_widget.update(markup)
        self._reactions_widget.display = bool(markup)