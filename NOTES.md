# Technical notes (13-step refactor)

Documentation of the *mechanical* adaptations needed to split up
`app.py`, with no behaviour change. If a bug is spotted along the way,
it is noted here and handled separately — never fixed during the refactor.

## Adaptations imposed by the split

### 1. Markup colors `ACCENT` / `DANGER` (mutable globals)
Originally: `app.py` defines `ACCENT` and `DANGER`, bounced on the fly by
`_apply_theme_globals()` (called at module import, in `ShelltrixApp.__init__`
and on every `cycle_theme`). Their consumers lived in the same module:
a `global ACCENT` reassignment was therefore seen everywhere.

After extraction, the consumers (ChatScreen, RecoveryDialog, SasDialog,
InviteDialog) are in other modules. A `from ..app import ACCENT` per module
would be *static*: the reassignment in `app.py` would no longer propagate
(stale theme bug). The structure forbids circular imports
`screens|dialogs → app`.

⇒ Adaptation: these call sites now read the live value
`themes.accent()` / `themes.danger()` at render time. This is strictly
equivalent to the old `ACCENT`: the invariant `ACCENT == themes.accent()`
(same for `DANGER`) is maintained by `_apply_theme_globals()` at import and on
every theme switch → no observable value changes.

`ACCENT` / `DANGER` / `_apply_theme_globals()` stay in `app.py`: they have
no consumer left but `ShelltrixApp` keeps maintaining them.

### 2. `_URL_RE` (URL regex) — removed from the initial list
`_URL_RE` is used by `ChatScreen._handle_incoming_message`. It is not
in the list of `formatting.py` functions, but it is a text formatting
constant and `chat.py` cannot import it from `app.py`
(circular). It therefore lives in `formatting.py`.

### 3. Sorting `SENDER_COLORS` / `SYNC_LABELS`
- `SENDER_COLORS`: used only by `_sender_color` → `formatting.py`.
- `SYNC_LABELS`: used only by `ChatScreen` → `screens/chat.py`.

### 4. Deferred imports during the migration (resolved)
During the split, `CommandPalette`/`StoreUnlockDialog` called
`ChatScreen`, `JoinRoomDialog`, `RecoveryDialog`, `LoginScreen` while they were
still in `app.py`; they used imports inside the function body to avoid any
return to `app.py`. Once each module was extracted (steps 5, 6, 11,
12), all these imports were moved back to the top of the module. `app.py`
only references "downstream" modules (screens/, dialogs/, config/,
matrix_client/) — no cycle.

### 5. `MatuiApp` → `ShelltrixApp`
The class name followed the project: sections 1 and 3 still spoke
of `MatuiApp`, while the class is called `ShelltrixApp`
(`src/shelltrix/app.py`). No code references the old name.

### 6. Timeline: `RichLog` → widgets
This is not a mechanical adaptation, it is a rendering model change
that had to be made for long messages, replies and reactions:
a `RichLog` only displays already-rendered text, so it can neither
fold, nor carry a reaction per message, nor become the target of a
`scroll_to_widget`.

The timeline is now a `VerticalScroll` (`#timeline`) filled with
`MessageView` (`widgets.py`), one widget per message. Consequences to
know about:

- any lookup of a timeline element goes through
  `query_one(..., MessageView)`, not a text line;
- `RichLog` is no longer imported in `screens/chat.py`;
- the fallback is measured on the *rendered* lines (actual panel width,
  markup stripped), not on the source `\n` — hence `timeline_width` and
  its `_FALLBACK_TIMELINE_WIDTH` fallback before the first layout;
- a mounted message is not sized yet: precise scrolls
  (`open_message`, repositioning after a history prepend) go through
  `call_after_refresh`.

## Final structure (src/shelltrix/)
- `app.py`: `ShelltrixApp`, `_apply_theme_globals`, `ACCENT`/`DANGER` globals
  (maintained but with no consumer), `run()`.
- `config.py`: paths, preferences, store encryption (Fernet), recovery key,
  first-run marker.
- `accounts.py`: multi-account (`accounts.json` + tokens in the keyring).
- `matrix_client.py`: `matrix-nio` wrapper (login, sync, send, SAS,
  reactions, upload) and the non-blocking first sync.
- `cache.py`: local SQLite cache of the messages, indexed by account and room.
- `formatting.py`: timeline blocks, dates, replies, reactions, markdown,
  URL regex.
- `sidebar.py`: ROOM / SESSION panels, frames and tree branches.
- `widgets.py`: `MessageView` (foldable body, reactions), send button.
- `themes.py`, `image_renderer.py`, `notifications.py`: color tokens,
  image rendering, desktop notifications.
- `screens/{login,chat,splash,account_picker}.py`, `dialogs/{command_palette,
  join_room,search,recovery,store_unlock,sas,invite}.py`: screens and dialogs.

## Bugs spotted during the refactor (to be handled separately)
- (none for now)
