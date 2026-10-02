# Notes techniques (refactor 13 étapes)

Documentation des adaptations *mécaniques* nécessaires au découpage de
`app.py`, sans changement de comportement. Si un bug est repéré en passant,
il est noté ici et traité séparément — jamais corrigé pendant le refactor.

## Adaptations imposées par le découpage

### 1. Couleurs markup `ACCENT` / `DANGER` (globals mutables)
À l'origine : `app.py` définit `ACCENT` et `DANGER`, rebondus à la volée par
`_apply_theme_globals()` (appel à l'import du module, dans `ShelltrixApp.__init__`
et à chaque `cycle_theme`). Leurs consommateurs vivaient dans le même module :
une réaffectation `global ACCENT` était donc vue partout.

Après extraction, les consommateurs (ChatScreen, RecoveryDialog, SasDialog,
InviteDialog) sont dans d'autres modules. Un `from ..app import ACCENT` par
module serait *statique* : la réaffectation dans `app.py` ne se propagerait
plus (bug de thème non rafraîchi). La structure interdit les imports
circulaires `screens|dialogs → app`.

⇒ Adaptation : ces call sites lisent désormais la valeur vivante
`themes.accent()` / `themes.danger()` au moment du rendu. C'est strictement
équivalent à l'ancien `ACCENT` : l'invariant `ACCENT == themes.accent()`
(et idem `DANGER`) est maintenu par `_apply_theme_globals()` à l'import et à
chaque bascule de thème → aucune valeur observable ne change.

`ACCENT` / `DANGER` / `_apply_theme_globals()` restent dans `app.py` : ils
n'ont plus de consommateur mais `ShelltrixApp` continue de les maintenir.

### 2. `_URL_RE` (regex d'URL) — retiré de la liste initiale
`_URL_RE` est utilisé par `ChatScreen._handle_incoming_message`. Il n'est pas
dans la liste des fonctions de `formatting.py`, mais c'est une constante de
formatage de texte et `chat.py` ne peut pas l'importer depuis `app.py`
(circulaire). Il réside donc dans `formatting.py`.

### 3. Sorting `SENDER_COLORS` / `SYNC_LABELS`
- `SENDER_COLORS` : utilisé uniquement par `_sender_color` → `formatting.py`.
- `SYNC_LABELS` : utilisé uniquement par `ChatScreen` → `screens/chat.py`.

### 4. Imports différés pendant la migration (résolus)
Pendant le découpage, `CommandPalette`/`StoreUnlockDialog` appelaient
`ChatScreen`, `JoinRoomDialog`, `RecoveryDialog`, `LoginScreen` encore dans
`app.py` ; ils utilisaient des imports dans le corps de fonction pour éviter
tout retour vers `app.py`. Une fois chaque module extrait (étapes 5, 6, 11,
12), tous ces imports sont repassés en haut de module. `app.py` ne référence
que des modules "aval" (screens/, dialogs/, config/, matrix_client/) —
aucun cycle.

### 5. `MatuiApp` → `ShelltrixApp`
Le nom de la classe a suivi le projet : les sections 1 et 3 parlaient encore
de `MatuiApp`, la classe s'appelle `ShelltrixApp` (`src/shelltrix/app.py`).
Aucun code ne référence l'ancien nom.

### 6. Timeline : `RichLog` → widgets
Ce n'est pas une adaptation mécanique, c'est un changement de modèle de rendu
qu'il a fallu faire pour les messages longs, les réponses et les réactions :
un `RichLog` n'affiche que du texte déjà rendu, donc il ne peut ni se
replier, ni porter une reaction par message, ni devenir la cible d'un
`scroll_to_widget`.

La timeline est désormais un `VerticalScroll` (`#timeline`) rempli de
`MessageView` (`widgets.py`), un widget par message. Conséquences à
connaître :

- toute recherche d'un élément de la timeline passe par
  `query_one(..., MessageView)`, pas par une ligne de texte ;
- `RichLog` n'est plus importé dans `screens/chat.py` ;
- le repli est mesuré sur les lignes *rendues* (largeur réelle du panneau,
  markup retiré), pas sur les `\n` de la source — d'où `timeline_width` et
  son repli `_FALLBACK_TIMELINE_WIDTH` avant le premier layout ;
- un message monté n'est pas encore dimensionné : les scrolls précis
  (`open_message`, repositionnement après un prepend d'historique) passent par
  `call_after_refresh`.

## Structure finale (src/shelltrix/)
- `app.py` : `ShelltrixApp`, `_apply_theme_globals`, globals `ACCENT`/`DANGER`
  (maintenus mais sans consommateur), `run()`.
- `config.py` : chemins, préférences, chiffrement du store (Fernet), clé de
  récupération, marqueur de premier lancement.
- `accounts.py` : multi-comptes (`accounts.json` + tokens dans le keyring).
- `matrix_client.py` : wrapper `matrix-nio` (login, sync, envoi, SAS,
  réactions, upload) et le premier sync non bloquant.
- `cache.py` : cache SQLite local des messages, indexé par compte et salon.
- `formatting.py` : blocs de timeline, dates, réponses, réactions, markdown,
  regex URL.
- `sidebar.py` : panneaux ROOM / SESSION, cadres et branches d'arbre.
- `widgets.py` : `MessageView` (corps repliable, réactions), bouton d'envoi.
- `themes.py`, `image_renderer.py`, `notifications.py` : tokens de couleur,
  rendu d'images, notifications desktop.
- `screens/{login,chat,splash,account_picker}.py`, `dialogs/{command_palette,
  join_room,search,recovery,store_unlock,sas,invite}.py` : écrans et dialogues.

## Bugs repérés pendant le refactor (à traiter séparément)
- (aucun pour l'instant)