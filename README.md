<p align="center">
  <code>shelltrix</code> — a premium Matrix TUI client
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License" />
  <img src="https://img.shields.io/badge/matrix--nio-E2EE-purple" alt="E2EE via matrix-nio" />
</p>

---

**shelltrix** is a terminal-based Matrix client built on
[`matrix-nio`](https://github.com/matrix-nio/matrix-nio) (E2EE via libolm)
and [`Textual`](https://textual.textualize.io/) (modern TUI framework).

A daily-driver replacement for gomuks / iamb, designed to be fast,
customizable, and fully owned by you — every behavior lives in this repo.

---

## Install

**Linux / macOS** with **Python >= 3.10** required.

```bash
curl -fsSL https://raw.githubusercontent.com/Nawal-alao/shelltrix/v1.0.3/install.sh | sh
```

The installer handles `libolm`, `pipx`, and the shelltrix package in one step.
The source is **pinned to the release tag** `v1.0.3`, which sits on the last
published commit: see `SHELLTRIX_REF` at the top of `install.sh`. Once
published, a pinned PyPI package will replace this path.

Verify:

```bash
shelltrix --version
```

### Manual install

```bash
# 1. libolm (pick one)
#    macOS         brew install libolm
#    Debian/Ubuntu sudo apt-get install -y libolm-dev
#    Fedora        sudo dnf install -y libolm-devel
#    Arch          sudo pacman -S --noconfirm libolm

# 2. pipx
#    Debian/Ubuntu sudo apt-get install -y pipx
#    Fedora        sudo dnf install -y pipx
#    Arch          sudo pacman -S --noconfirm python-pipx
#    macOS         brew install pipx

# 3. shelltrix
pipx install "git+https://github.com/Nawal-alao/shelltrix.git@v1.0.3"
```

### Windows

No native build. Install inside [WSL2](https://learn.microsoft.com/windows/wsl/install)
(Ubuntu recommended) and run the commands above from your WSL terminal.

---

## Usage

```bash
shelltrix
```

On first launch, a welcome splash plays, then a login screen asks for your
homeserver, user ID, and password. Credentials are stored in
`~/.config/shelltrix/` with `600` permissions — every later launch skips the
splash and goes straight to the chat interface.

### From a local checkout

```bash
# Option A — alias
echo "alias shelltrix='$(pwd)/.venv/bin/shelltrix'" >> ~/.bashrc

# Option B — symlink (works in scripts)
ln -s "$(pwd)/.venv/bin/shelltrix" ~/.local/bin/shelltrix
```

### Cutting a release

The version lives in six places, so it is bumped in one command:

```bash
./bump.sh 1.0.2          # rewrite pyproject, __init__, install.sh and README
./bump.sh 1.0.2 --tag    # ... and create the annotated tag
```

The script anchors on the *previous* version and fails loudly if an expected
occurrence is missing, refuses to bump without a `## [1.0.2]` changelog
section, and runs the supply-chain tests before tagging. A release whose pin
disagrees with the package version cannot pass CI: see
`test_pinned_ref_matches_the_declared_version`.

Then review, and push **the tag first**:

```bash
git commit -am "chore(release): bump version to 1.0.2"
git push origin v1.0.2
git push origin main
```

---

## Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+P` | Command palette |
| `Ctrl+R` | Focus room list |
| `Ctrl+L` | Focus composer |
| `Ctrl+F` | Search in local history |
| `Ctrl+K` | Clear active timeline |
| `Ctrl+D` | Toggle sidebar |
| `Ctrl+Q` | Quit |
| `Enter` | Send message (in composer) |
| `↑` / `↓` + `Enter` | Navigate & open room |
| `PageUp` / `PageDown` | Scroll the timeline, page up loads older history |
| `/help` | List the slash commands |

---

## Command palette

Open with `Ctrl+P`. Type to search instantly; a **Suggested** section surfaces
the three most frequent actions, then four themed sections:

| Section | Commands |
|---------|----------|
| **Navigation** | Focus rooms, focus composer, toggle sidebar |
| **Chat** | Clear screen, mark as read, search messages, join room |
| **Action** | Insert `/sendimg`, open last link |
| **System** | Sync status, switch theme, recovery key, switch account, sign out, quit |

Each row shows its keyboard shortcut, and the dialog carries a title bar with
the `esc` reminder. Navigate with `↑`/`↓` or `Ctrl+P`/`Ctrl+N`, confirm with
`Enter`, close with `Esc`.

---

## Slash commands

Type `/` in the composer to trigger fuzzy autocompletion:

| Command | Description |
|---------|-------------|
| `/me <text>` | Send an action (italic emote) |
| `/reply <text>` | Reply to the last received message |
| `/react <emoji>` | React to the last received message |
| `/reactions [event_id]` | Reload a message's reactions from the server |
| `/join <#alias>` | Join a room by alias |
| `/sendimg <path>` | Send an image from disk (E2EE) |
| `/search <text>` | Search local message history |
| `/recovery` | Show / regenerate E2EE session key |
| `/theme` | Switch theme |
| `/quit [farewell]` | Leave the room |
| `/help` | Show command list |

`@` and `#` in the composer also trigger mention / room completion.

---

## Timeline

Every message is its own widget, which is what makes the rest possible:

- **Collapsed long messages** — anything past ~5 rendered lines is clipped,
  with a *see more* footer to expand it (and *see less* to fold it back).
- **Hour gutter** — the time sits in a left column, the message body on the
  right, instead of a `HH:MM ────` rule interrupting the text.
- **Day separators** — `today`, `yesterday`, or a full date, inserted between
  two days.
- **Replies** — `/reply` sends a real `m.in_reply_to` relation; the cited
  message is rendered above the answer, with an `↪ Name` fallback when the
  target is unknown.
- **Reactions** — totals under the message, updated live and rebuilt from the
  server history when a room is reopened.

---

## Sidebar

A context panel on the right (`Ctrl+D`):

- **Room** — name, alias, topic, member counts, encryption state, your
  power level (`Admin (100)` / `Moderator (50)` / `User`).
- **Session** — sync state (colored indicator), last refresh age.

Both panels are drawn as framed blocks (`┌ ROOM ┐`), and the room list reads
as a tree (`├─ room`) so long names stay legible.

---

## Themes

Two built-in themes, switchable live with `/theme`:

| Theme | Accent | Description |
|-------|--------|-------------|
| **opencode** | `#e59e72` | Warm dark (default) |
| **matrix_green** | `#50fa7b` | Deep green |

Persisted in `~/.config/shelltrix/config.json`. All CSS colors come from
theme variables — add a new theme by appending an entry to
`src/shelltrix/themes.py`.

---

## Features

### Core

- Login + continuous sync loop
- Room list, live timeline, message sending
- Replies (`m.in_reply_to`) and reactions, live and from history
- Receiving & decrypting encrypted messages (E2EE)
- Device verification by emoji (SAS) with manual confirmation
- Refuse to send to unverified devices
- Accept/Decline dialog for room invitations
- Clean logout: server-side token revocation + local credential wipe

### Security

- Access token stored in the **system keyring**, never in plaintext
- E2EE store encrypted at rest (Fernet, key in keyring)
- **Session recovery key** (`/recovery`): shareable secret for E2EE
  history restoration; only a scrypt verifier lives on disk
- Encrypted store auto-decrypted on startup via keyring or recovery key
- Encryption aborts loudly rather than leaving session keys in plaintext

### Interface

- ASCII art welcome splash, on the very first launch only
- Centered card login with styled errors
- Top status bar: room name, sync indicator, clock
- Room list as a framed tree, sorted by unread, with badges
- Timeline of per-message widgets: conversation grouping, collapsed long
  messages, hour gutter, day separators, inline markdown, truecolor
  half-block image previews
- Framed sidebar with room & session context
- Title bars on the command palette and the search dialog

### Reliability

- **Automatic reconnection** with exponential backoff (1s → 30s)
- **Instant startup**: the first sync runs in the background, the room list
  fills in as soon as the answer lands
- **Server history / scrollback**: older messages loaded on PageUp, view
  position preserved across prepends
- Local message cache (`~/.config/shelltrix/cache/`) for instant re-open
- Local full-text search (`Ctrl+F`), jumping straight to the message
- Direct-mention detection with a distinct `@` badge per room

---

## Roadmap

1. Sixel / Kitty graphics protocol for inline images (today: truecolor
   half-blocks)
2. Cross-signing device verification — blocked by `matrix-nio`. Direction:
   move the core to `matrix-sdk` (Rust) behind the existing Textual UI
3. Message editing and threads

---

## License

[MIT](LICENSE)
