# Changelog

All notable changes to shelltrix. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versioning adheres to
[SemVer](https://semver.org/).

## [Unreleased]

### Changed
- **The Matrix facade no longer knows about matrix-nio.** `ShelltrixClient` keeps
  every method signature, and each one now dispatches through a transport
  (`src/shelltrix/transport.py`) instead of reaching for a library client:
  matrix-nio behind it in `nio_transport.py`, the Rust core behind it in
  `rust_transport.py`. The seam existed but the facade still imported nio,
  registered nio callbacks and normalized nio objects, so migrating an operation
  meant editing the class the whole UI is written against.
- A refused operation on the Rust backend no longer lists everything that is
  still missing ("sending, uploading, room changes…"). That list was true when it
  was written and a lie the moment the next operation landed; it now says what
  the user was doing and how to get back to matrix-nio.
- An encrypted room with an unverified device is reported as "not delivered"
  through shelltrix's own refusal type, instead of the UI having to understand
  matrix-nio's `LocalProtocolError`.
- Two copies of the reconnecting sync loop became one. A fix to reconnection
  previously had to be written twice, and nothing stopped it from being written
  once.

### Fixed
- The header state flickered between "syncing" and "online" every 30 seconds on
  a healthy connection. Both sync loops set "syncing" at the top of every
  iteration; the state is now announced once, on entry, and the loop only reports
  outcomes afterwards.

## [1.0.2] - 2026-10-02

### Fixed
- `install.sh` rejected the ref it pins itself: validation accepted
  hexadecimal SHAs only, while the shipped pin is the release tag
  `v1.0.1`. `curl … | sh` therefore aborted with `ERROR Invalid
  SHELLTRIX_REF: 'v1.0.1' (hexadecimal commit SHA required)` on every
  machine. The ref check now accepts a release tag or a commit SHA.
- The ref is now validated **before** anything is installed, instead of
  after libolm and pipx. An invalid `SHELLTRIX_REF` used to modify the
  machine and only then abort.

### Added
- `sh install.sh --check-ref <ref>` runs the ref validation alone, so
  the test suite exercises the shipped code instead of a copy of it.
- Three tests: the installer accepts its own pin, it refuses unsafe
  refs, and the check precedes every side effect.


## [1.0.1] - 2026-10-02

### Changed
- Own messages now display as `You` in the timeline (previously `Vous`). The
  whole user-facing surface — UI, installer output, errors — is now English.
- The installer's own output is English, matching the UI it installs.

## [1.0.0] - 2026-10-02

### Added
- **Replies**: the `/reply` command replies to the last message received in the
  current room. The sent message carries the `m.relates_to` expected by the
  Matrix spec (`m.in_reply_to`), falling back to `↪ Name` when the target is
  unknown or out of cache. The cited message is restored from history and
  displayed inside the reply.
- **Reactions**: `/reactions` lists a message's reactions; `/react <emoji>` adds
  one. The total is displayed under the message and refreshed live, or rebuilt
  from history when a room is reloaded.
- **Widget timeline**: each message is a `MessageView` widget rather than a
  `RichLog` line. Long messages are folded after a few lines ("see more" to
  expand), the time shows in a left gutter, and a day separator (`today`,
  `yesterday`, `monday, march 4`) is inserted between two days.
- **First launch only**: the welcome splash appears only the first time; later
  launches go straight to the room list.
- **Sidebar frames**: the ROOM and SESSION sections are framed, the room list is
  presented as a tree (`├─`), and the command palette and search gain a title bar
  with a shortcut reminder.
- **Non-blocking startup**: the first sync runs in the background, the screen
  appears immediately, and the room list populates as soon as the response
  arrives.
- **Grouped conversational timeline**: consecutive messages from the same sender
  are grouped into blocks with a single `› You` / `‹ Name` indicator (instead of a
  name repeated on every line, system-log style). A time separator (`HH:MM ────`)
  appears after ~5 min of silence. Rendering is computed at display time from
  structured entries, which allows a consistent re-render when a room is opened.
- **Server history / scrollback**: when opening a room, shelltrix loads the most
  recent messages from the server, then walks back into the past on each upward
  scroll to the top of the timeline (`PageUp`). Duplicates (messages already
  received via sync) are deduplicated by `event_id`, the scroll position is
  preserved, and `Ctrl+K` clears the room.
- **Complete structured message model**: each timeline entry now carries
  `event_id`, `msgtype` and `has_mention` (`@user` detection) on top of
  sender/date/time. This is the foundation for pagination, search, mentions and
  persistence.
- **Direct mentions highlighted**: a message mentioning you (`@you` or
  `@you:server`) has its mention displayed in bold and accent color in the
  timeline, and the room shows a distinct `@` indicator in the room list until
  the mention is read (desktop notifications are then prefixed with
  `@Mention ·`). Safe detection, no false positives (`@bob2`, `@bob:other`).
- **Local SQLite cache**: received messages (sync + scrollback) are persisted per
  room and per account in `~/.config/shelltrix/cache/` (file mode 0600). When
  reopening an already-seen room, history displays instantly from the cache
  before the server even responds; re-deduplication by `event_id` prevents
  duplicates. `Ctrl+K` also purges the room's cache.
- **Local message search** (`Ctrl+F`, `/search`, "Search messages" palette): a
  search modal scans the cached history of the rooms, case-insensitive, and
  displays matches (room + excerpt + time). Selecting a result opens the room
  and positions the timeline on the message.
- **100% Textual-safe image rendering**: no escape sequence is written to `stdout`
  anymore (which corrupted the full-screen display). The image is decomposed
  into colored Unicode half-blocks (truecolor) written into the RichLog; works
  on any 24-bit terminal. Simple `📷 Image` fallback if Pillow is missing. Pillow
  becomes an optional dependency (`shelltrix[image]`).
- Automatic reconnection with exponential backoff (1s → 30s) on network failure:
  the app re-syncs by itself instead of staying "offline". New `reconnecting…`
  state in the header and sidebar.
- `Tab` completion for fuzzy suggestions (slash commands, `@user` / `#room`
  mentions), in addition to `Enter`.
- Inline strikethrough (`~~text~~`) in markdown rendering.
- Integration tests for the `ShelltrixClient` layer (send security policy,
  invites, typing, image sending) by mocking `nio.AsyncClient`.
- Complete PyPI metadata (license, classifiers, URLs, keywords) and CI
  verification of the wheel contents (`app.tcss` present).

### Fixed
- Opening a message from search (`Ctrl+F` → `Enter`) positions the timeline on
  the right message instead of jumping to the top of the room.
- The view no longer jumps to the bottom of the timeline when a history prefix
  arrives: the scroll position is realigned once the widgets are mounted.
- The "alias" field of the "Join a room" modal joins the room with `Enter`, like
  the button.
- Error messages intended for the end user are in English (exit code
  conventions), instead of a French/English mix.
- `app.tcss` is now bundled in the package (`setuptools.package-data`): the file
  no longer disappears after `pip install`.
- The installer uses the system package manager for `pipx` (`sudo apt install -y
  pipx` on PEP 668 distros) instead of `pip install --user`, which breaks on
  Debian 11+ / Ubuntu 23.04+.
- `@user` mentions now exclude the current user.
- The unread badge is capped at `99+` instead of an overflowing number.

## [0.2.0] - 2026-09

### Added
- Typing indicators (`typing…`) in the timeline.
- Animated splash with a fixed ASCII logo (no more `pyfiglet` dependency).
- Redesigned command palette (section headers, badges).
- Removal of the web runtime.

### Fixed
- `_handle_typing` is now async (matching `TypingHandler`): typing events no
  longer raise `TypeError`.
- Status interval timers are stopped on unmount (no more leak).
- Palette: use the color token for section headers.

## [0.1.0] - 2026

### Added
- Matrix login + continuous sync loop.
- Room list, live timeline, message sending.
- E2EE encryption (receive/decrypt).
- Device verification by emoji (SAS) with human confirmation.
- Blocking sends to unverified devices in an encrypted room.
- Human confirmation of invites.
- Access token stored in the system keyring.
- E2EE store encrypted at rest (Fernet, key in the keyring) + session recovery
  key (`/recovery`).
- Command palette, clean logout (token revocation).