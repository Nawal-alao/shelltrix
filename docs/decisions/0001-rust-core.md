# ADR 0001 — Move the core to Rust, keep Textual

- **Status**: Accepted. Packaging resolved on 2026-10-02 (see *Resolved —
  packaging*). The Rust backend is wired into the client and **read-only**:
  syncing and display work, everything that writes is refused. E2EE is not
  started.
- **Date**: 2026-10-02
- **Scope**: architecture and migration status.

## Context

`matrix-nio` has had no meaningful release for roughly two years, and the
README roadmap lists cross-signing device verification as *blocked by
`matrix-nio`*. So the Rust move is not a performance project: it is the key to
the feature roadmap, and a project-longevity problem.

Performance is explicitly **not** the driver. A Matrix client is bound by the
network (TLS, `/sync`, E2EE), not by CPU. The measured numbers live in
`bench/core_hotpaths.py`; if the projected Rust gain is below 10 % of wall
time, this decision is justified by longevity alone and should be documented
that way.

The frontend is **not** being migrated. Textual stays, including the timeline
as widgets.

## Decision 1 — In-process PyO3 extension, not a daemon

The Rust core is a native Python extension built with maturin. Not a
sidecar process behind a socket.

- PyO3 wraps the FFI call in `catch_unwind` and turns a Rust panic into a
  Python exception, so the crash-isolation argument for a daemon mostly
  evaporates.
- Single artifact, no supervisor, no protocol, no second event loop
  (Tokio alongside asyncio).
- The serialization a daemon would add is microseconds per event against
  network latencies of tens of milliseconds: invisible.

**Reversal condition**: a second frontend needs to consume the core (mobile,
web). Then a daemon becomes the right shape. The facade boundary required by
decision 3 exists precisely so this reversal stays cheap.

## Decision 2 — The core owns the network and the crypto, nothing else

`src/shelltrix/matrix_client.py` (496 lines, 17 % of the code) migrates to Rust,
plus the ~20-line at-rest encryption block for the olm store in
`config.py`, which travels with it. Splitting an encryption key from its store
would be a security bug that only shows up in use.

That file is now three (`transport.py`, `nio_transport.py`,
`rust_transport.py`, see step 3.4), because "what matrix-nio looks like" and
"what shelltrix asks of a Matrix library" stopped being the same thing the
moment a second backend existed. The facade is 446 lines, matrix-nio behind it
435.

`cache.py` (301 lines) does **not** migrate yet. Two stores writing the same
data diverge silently, and rewriting the UI read layer is the real risk of the
project. The Python cache stays the single source of truth for the UI; the core
runs with an in-memory store.

Everything else — `formatting.py`, `image_renderer.py`, `screens/`, `dialogs/`,
`widgets.py`, `sidebar.py`, `themes.py`, `app.py`, ~83 % of the code — never
moves.

## Decision 3 — The Python facade does not change

`ShelltrixClient` keeps its method signatures. Only its body moves to Rust.
Consequences, which are the whole point:

- `widgets.py`, `app.py`, `sidebar.py`, `formatting.py` are untouched.
- The tests pass unmodified. A test failure means the facade diverged, so
  the interface is verified on every run.

This is also why the bench and the tests are the acceptance criteria, and why
"is the migration finished?" has a numeric answer instead of an impression.

Not a mechanical translation: `AsyncClient` is callback-driven, `matrix-sdk`
emits an event stream. The 496 lines are replaced *behind a contract written
first*.

## Decision 4 — Opt-in, with a written promotion criterion

`SHELLTRIX_CORE=python|rust`, default `python`. The facade exists whichever
backend is active, so both are testable side by side.

Rust becomes the default after **N consecutive releases at parity**, not
"once it works". That release is a **minor** (1.5.0): the observable default
changes. Removing the Python core later is a **major** (2.0.0): it removes a
supported path.

The app version and the core version are independent. The app ships a pinned,
known-good core build; `shelltrix 1.4.0` must work whatever the core's own
version. The core may iterate rapidly through its own 0.x during the migration.

## Resolved — packaging (was the blocking open question)

As soon as Rust is in the install path, `curl … | sh` breaks for anyone
without cargo. **Resolved by measurement, on 2026-10-02.**

The extension is built `abi3-py310`: **one wheel serves CPython 3.10 through
3.14**. The wheel matrix stays at one entry per platform instead of one per
(platform, minor), which is what made the PyPI option look expensive. Verified
end to end — `maturin build`, wheel install, `import`, call — and the job
`rust-core` in CI rebuilds it on every push.

So the answer is option 1, prebuilt wheels on PyPI, and `install.sh` does not
change: it already installs through `pipx`/`uv tool install`, which resolves
the optional `shelltrix-core` dependency like any other. The user never needs
cargo.

`Cargo.lock` is committed, so a wheel is built from pinned dependency versions
and a release stays reproducible.

Two things this leaves open, both smaller:

- Publishing wheels for the other platforms (macOS, Windows). The Linux wheel
  is proven; the release workflow does not exist yet.
- Until the core is the default, `shelltrix-core` is *not* installed by
  `install.sh`. The opt-in path is `pipx install shelltrix-core` plus
  `SHELLTRIX_CORE=rust`.

## Progress

Migrated so far, each step verified:

| Step | State |
|---|---|
| Packaging question | resolved, abi3 wheel proven in CI |
| Seam (`shelltrix._core`), backend selection, parity tests | done, 325 tests |
| `/sync` parsing → timeline messages | done, 30x measured |
| Feasibility against a real homeserver (step 3.0) | done, loop closed |
| Transport seam: UI free of nio (step 3.1) | done, enforced by a test |
| Rust transport: login + one /sync from Python (step 3.2a) | done |
| Rust sync loop as an event stream (step 3.2b) | done |
| Rust event classifier & dedup (step 3.2c) | done, messages + images + reactions + typing + invites |
| Sync filter for typing / invites (step 3.2d) | done, explicit m.typing filter added |
| Token restore instead of password login (step 3.3a) | done, `start_sync_with_token` |
| Room names resolved in Rust (step 3.3b) | done, `rooms_snapshot` |
| Seam wired into `ShelltrixClient` (step 3.3c) | done, **read-only**: syncs and displays |
| Transport contract, one door per operation (step 3.4) | done, `matrix_client` free of nio |

## The plan — étapes 0 to 6

Written down so the sequence survives a session. Étapes 0 to 3 are the migration
to *parity*; 5 is the one that is not a migration at all but a feature the
migration gates; 6 is what makes the core the default.

| Étape | ADR step | Delivers | State |
|---|---|---|---|
| 0 | 3.4 | `Transport` contract, `NioTransport`, `RustTransport`, facade free of nio, one sync loop | **done** |
| 1 | 3.5 | Sending text, emote, reaction, reply (`send_event` in Rust) | next |
| 2 | 3.6 | History: `room_messages`, then `next_batch` | not started |
| 3 | 3.7 | Room changes: join, accept, decline, leave, `fetch_reactions` | not started |
| 4 | 3.8 | Image upload (`media().upload()`) | not started |
| 5 | 3.9 | E2EE: crypto store, Olm machine, key management, SAS | not started |
| 6 | 4 | Real `logout` (token revocation), then promotion after N releases at parity → 1.5.0 | not started |

Two constraints that are not obvious from the order:

**Steps 1 and 4 refuse in encrypted rooms.** The core can sync an encrypted room
and see the `m.room.encryption` state event, but it has no crypto store, so it
cannot encrypt what it sends: a message posted to `m.room.message` in an
encrypted room would arrive as unreadable plaintext-to-the-room, i.e. visible to
the server and to nobody else — worse than refusing. So `RustTransport` raises
`UnsupportedOperation` when the target room is encrypted, naming the room, and
the test asserts **nothing was sent** rather than merely that it raised. The
guard comes off in step 5, when the store exists.

**Step 5 is not a port, it is a dependency.** matrix-sdk's crypto support is
behind the `e2e-encryption` feature, which this crate deliberately does not enable
by default: shipping an Olm machine with no key store and no way to verify
anything is the worst of both. So step 5 starts by finding out what 0.19 actually
offers — store, machine, key management, SAS — and the answer decides whether
cross-signing lands here or the roadmap item it gates moves elsewhere.

### Findings deferred to their own commit

Deliberately not touched by étape 0, because each is a behaviour change and this
step is a refactor:

| Where | Finding | Why it was left |
|---|---|---|
| `transport.py:286` | `BLE001` blind `except Exception` in the sync loop | Intended: one long poll may fail in any way, and the backoff is the answer. Rewriting it as a narrower catch is a judgement call, not a cleanup. |
| `matrix_client.py:164` | `S110` + `BLE001` `try`/`except`/`pass` in `_fire_first_sync` | A UI refresh error must not be mistaken for an outage. It deserves a `log.exception`, which is a behaviour change. |
| `matrix_client.py:247,345,418` | `BLE001` in `_send`, `send_image`, `room_messages` | The facade reports any failure to the user; a narrower catch would let some failures go unreported. |
| `nio_transport.py:298` | `BLE001` + `S110` around `client.logout()` | Same, plus the local cleanup must happen even offline. |
| `nio_transport.py:137` | `TRY004` raise `SendRefused` after an `isinstance` check | A false positive: the refusal is the homeserver declining an upload, not a caller passing a wrong type. |

### What "wired in" means, and what it does not

`ShelltrixClient` now builds a real transport when `SHELLTRIX_CORE=rust`: it
restores the saved session, drains the core's event queue, resolves room names,
and dispatches to the same handlers the UI already had. Messages, images,
reactions, typing and invites all arrive, a quiet room reopens its `/sync`
instead of going silent, and an account nobody has spoken in still gets its room
list.

What it does **not** do is write anything. Every operation the core has not
migrated raises `_core.UnsupportedOperation` naming what the user was trying to
do and how to get back to matrix-nio. That is a deliberate reversal of the
earlier draft, which set `self.client = None` for the Rust backend while leaving
every method reaching for it: `SHELLTRIX_CORE=rust` looked alive and then died
on first use with `AttributeError: 'NoneType' object has no attribute
'room_send'`, with no test to notice. A visible refusal is the honest state of a
half-migration; a silent crash is not.

Two refusals are load-bearing rather than cosmetic:

- A refused send does **not** go through `on_send_error`. That callback means
  "the server would not accept this", and reporting a missing implementation
  through it would tell the user their message was rejected when nothing was
  ever sent.
- `room_messages()` returns `None` on failure, which the timeline reads as "no
  messages". So the refusal travels out as an exception instead of becoming an
  empty room.
- `logout()` is refused *before* it erases anything. It invalidates the token
  server-side first; deleting the local copy of a token the core cannot revoke
  would leave the session alive and unloggable.

Encrypted rooms are a related gap with the same shape: `supports_e2ee()` is
`False`, so the client says so at startup rather than letting them appear as
empty rooms.

The wiring is tested with a fake core (`tests/test_rust_backend.py`), not a
homeserver, because the bug above is a wiring bug and a test needing a
homeserver is a test nobody runs. The core's own behaviour stays covered by its
Rust tests and by `tests/test_rust_core.py`.

## Step 3.0 — feasibility spike (done 2026-10-02)

Before migrating anything, the question "can a Rust client talk to a real
homeserver at all" was answered with evidence rather than confidence:

- Real Synapse 1.162.0, local, SQLite, on `localhost:8008`.
- `matrix-sdk` 0.19.1, built with rustc 1.99 (its MSRV is 1.96).
- A Rust client logged in, got a real device ID, ran `/sync`
  (`200`, processed in ~18 ms), created a room, sent a message.
- A second Rust client saw that room after its own sync.
- The message was then read **out of Synapse's own SQLite database**, so the
  proof does not depend on matrix-sdk being correct about itself.

The loop is closed. The migration is feasible.

### What the spike changed in the plan

Three findings that the design has to respect:

1. **`Client::sync()` never returns.** It is the client's main loop, by
   design (`sync_with_callback(..., |_| LoopCtrl::Continue)`). So the transport
   cannot be "call sync and get events": it must run the loop as a background
   task and expose an event stream to Python. This is the single biggest
   difference from matrix-nio's callback model, and it confirms decision 3 was
   the right call — the facade hides it.

2. **matrix-sdk owns a Tokio runtime; the extension is embedded in
   asyncio.** A PyO3 module must never construct a runtime inside the Python
   thread. The core will run Tokio on a dedicated thread, and every call from
   Python will enter it with a guard. This is decided here, before any code
   exists, because getting it wrong deadlocks the UI.

3. **matrix-sdk 0.19 is pre-1.0 and its API moves.** Login is
   `client.matrix_auth().login_username(...)`, room creation takes a raw
   `create_room::v3::Request`, and `send_text` no longer exists. The seam is
   therefore more valuable than it looked: it is what absorbs this churn.

### Step 3.1 — the seam, done

Before a Rust transport can exist, the UI has to stop depending on matrix-nio,
because the two libraries disagree about almost everything: nio hands out
callbacks with its own event objects, `matrix-sdk` runs a loop and exposes a
stream of its own types.

`shelltrix.events` is now the contract: `Room`, `MessageEvent`, `ImageEvent`,
`MessagePage`. The rule is that a transport RESOLVES and the UI CONSUMES —
`Room.user_names` already holds disambiguated names, so nio's "Alice
(@bob:hs)" rules are applied once at the boundary rather than copied.

What that removed: eight `self.client.client.user_id` reaches into nio from
the UI, a `from nio import RoomMessageImage` in the middle of a handler, and
`room_messages()` handing a raw `RoomMessagesResponse` to the timeline.
`test_no_module_above_the_transport_imports_nio` now enforces this, and it
caught a leak in `dialogs/invite.py` the moment it was written.

The seam is therefore closed on the Python side.

### Step 3.2a — Python can drive the Rust core

`login_and_sync(homeserver, user, password)` in the core: log in, run one
`/sync`, return a plain `SyncSummary`. Called from Python through
`shelltrix._core`, against the local Synapse, it returns a real device ID and
the joined room in ~0.7 s.

The part that mattered was not the login. It was the GIL. matrix-sdk waits on
the network inside a call Python made, so:

- `runtime.rs` puts the Tokio runtime on a dedicated thread, started once and
  parked forever. It is never built on a Python thread.
- `lib.rs` wraps every call in `py.detach` (renamed from `allow_threads` in
  PyO3 0.29), so the caller parks with the GIL released.

Measured against the real homeserver: a 404 ms blocking call, 39 event-loop
ticks where ~40 were expected. The loop kept its full rate, so Textual keeps
repainting. That is now a test — against a stub homeserver that stalls, so it
needs no network — and it is the assertion to protect: if the GIL were ever
held again, the symptom would be a UI freeze blamed on something else.

`matrix-sdk` also raised the crate's MSRV to 1.96, and the release build takes
~20 minutes on a cold cache. Both are costs worth knowing before widening the
wheel matrix.

### Step 3.2b — the sync loop as a queue Python drains

`_core.start_sync(homeserver, user, password)` logs in, spawns the sync loop as
a Tokio task and returns immediately; `_core.next_event(timeout_ms)` hands the
next event to Python; `_core.stop_sync()` tears it down. Three properties drove
the shape, and each is a test rather than a comment:

**No Rust→Python callbacks.** A callback would have to reacquire the GIL from
whatever thread the sync loop runs on, so a slow repaint or a Textual widget
touching Python state from the wrong thread could deadlock against the Python
code waiting on `next_event`. A queue has no such cycle: Python pulls, Rust only
pushes. The queue is unbounded so a busy room cannot silently drop messages —
the UI is the only consumer and it drains continuously.

**No lock held across an `await`.** `next_event` parks while waiting for an
event, so holding the global stream lock there would block `stop_sync` until the
wait expired — which is how quitting the app would hang. The channel has its own
`tokio::sync::Mutex`, and the global lock is taken only long enough to clone an
`Arc` out of it. `start_sync` likewise logs in before installing itself in the
slot, so a slow homeserver cannot wedge a concurrent `stop`. Clippy caught this;
it is now the shape of the code, with a test.

**De-duplication at the queue, not the loop.** A resumed sync replays whatever
arrived while disconnected. Filtering by event ID as events are enqueued — not
where they are produced — means the guarantee holds for every producer and can
be tested without a homeserver, since `Queue` no longer depends on `Client`.

The queue is a separate type from the sync loop for the same reason: ordering,
de-duplication, the timeout that reports a quiet room as `None` rather than an
error, and the error a failed loop leaves behind are all verifiable offline.

Verified end to end against Synapse 1.162: a message written *before*
`start_sync` arrives through the initial sync, a message written *by another
client* while the loop runs arrives within the timeout, neither is replayed,
and a quiet room reads as `None` rather than an error. Script:
`~/.spike/stream-e2e.py`.

Not yet covered at this step: only `m.room.message` was normalized. Images,
reactions, typing and invites now cross the same seam too (step 3.2c), and
`matrix_client.py` consumes them (step 3.3c).

Recipe to reproduce: a Synapse venv (`matrix-synapse`, Python 3.12, SQLite),
`register_new_matrix_user -c homeserver.yaml -a`, then a cargo bin depending
on `matrix-sdk = "0.19"`.

### Step 3.3 — the client drives the core, for reading

Three things had to be true before the facade could run on Rust at all.

**The token, not a password.** The login path of 3.2a took a password because
that is what a fresh spike has. A real `shelltrix` has a saved access token and
device ID, and asking for the password again would have been a regression in the
eyes of the one person using it. `start_sync_with_token` restores the session
instead.

**Room names, resolved where the members are.** nio hands out
`MatrixRoom` objects with a computed `display_name`, and the UI reads that
directly. The core therefore computes names rather than shipping raw room IDs:
m.room.name, then the canonical alias, then matrix-sdk's "Alice and Bob" form,
and only then the ID. The algorithm lives in `rooms.rs`, and the fallback order
is covered by Rust tests because it is the part a user notices when it is wrong.
Events that arrive before the room is named still get a `Room` — showing a raw
ID for a moment beats dropping the message that named it.

**First sync asked, not inferred.** The client fires `on_first_sync` when the
core reports its initial `/sync` complete, rather than when the first event
arrives. On an account nobody has spoken in, no event ever arrives, and a
room-list refresh keyed off events would leave the sidebar blank forever on a
perfectly healthy connection. It is also asked after every poll, not after every
event, so a quiet long poll does not re-announce the sync and repaint the room
list every 30 seconds.

Reconnection needed one transport-specific adjustment. matrix-nio's loop
recovers by calling `sync()` again; the core owns its own sync task, so when its
queue reports the loop ended the client tears it down and starts a new one. Same
contract and same exponential backoff as the nio path, one different step.

### Step 3.4 — one door per operation, and the facade closed

Until now the seam existed but the facade still knew about the libraries behind
it. `ShelltrixClient` imported nio, built the nio client, registered nio
callbacks, and normalized nio objects — so migrating an operation meant editing
the facade, in the middle of the class the whole UI is written against. The
immediate symptom was `_nio(operation)`, a method whose entire job was to raise
`UnsupportedOperation` for everything the Rust core had not done yet: every
remaining operation would have added a branch there.

`transport.py` is now that door. `Transport` declares the operations, refuses
each one it has not implemented, and owns the one thing both backends do
identically — keeping a `/sync` alive forever with exponential backoff.
`nio_transport.py` is matrix-nio behind it, `rust_transport.py` is the core
behind it, and `matrix_client.py` imports neither.

Four things fell out of the split rather than being aimed at:

**The refusal message stopped having to be maintained.** It used to list what
was still missing ("sending, uploading, room changes, history…"), which was true
when written and a lie the moment the next operation landed. It now says what
the user was doing and how to get back to matrix-nio, and stays true.

**nio's exceptions stopped crossing the boundary.** The send path translated
`LocalProtocolError` — an encrypted room with an unverified device — into
`SendRefused`, so the security policy is expressed in shelltrix's own terms and
the facade can report a refusal without naming a library. The same happened to
the upload response and to `room_messages`, which no longer has to know what a
`RoomMessagesResponse` is.

**The duplicated sync loop became one.** Two copies of the backoff loop was a
reconnection fix applied to one backend only. The difference is now two methods:
`_poll` (one long poll) and `_synced` (has a sync completed). The Rust one raises
`RestartSync` after rebuilding its core, which is a reconnection and not a
failure, so it does not pay a backoff for it.

That unification found a bug on the way. Both loops set `sync_state = "syncing"`
at the top of every iteration, so a healthy connection alternated between
`syncing` and `online` in the header every 30 seconds — a flicker nobody sees
because it is invisible in a screenshot. The state is now announced once, on
entry, and afterwards the loop only reports outcomes.

**The nio-free invariant now covers the facade.** `test_events.py` allowed
`matrix_client.py` to import nio, on the grounds that it *was* the transport.
It is not any more, and a stray `from nio import …` there would pass the old
allowlist while quietly re-coupling every screen to the library the core
replaces. The allowlist is now one file, `nio_transport.py`, and a second test
asserts the facade stays outside it.

What it cost: fifteen test lines, all of them `patch("shelltrix.matrix_client.
AsyncClient")` becoming `patch("shelltrix.nio_transport.AsyncClient")`, plus the
two tests that called a handler or a dispatch method directly and now call the
transport's. No assertion changed. What it buys is that steps 3.5 onwards are one
method on `RustTransport` plus a facade that already dispatches to it.

`RustTransport` still implements seven of the seventeen operations: `close`,
`rooms`, `next_batch`, `load_local_store`, `supports_e2ee`, and the two sync
hooks. Everything else refuses by name. `test_both_backends_implement_the_same_
operations` guards the shape of that gap — the operations refuse loudly, but a
forgotten override of the ones with defaults (`rooms()` would read as an empty
sidebar) is silent, and this is the test that says so.

## Consequences

- The roadmap item "cross-signing, blocked by matrix-nio" unblocks only via
  this migration, so it gates the roadmap rather than running beside it.
- The Python core stays alive for the whole migration. No deletion before 2.0.
- Benchmarks must exist before the migration, otherwise "faster" is a claim
  nobody can check.

## Rejected

- **Full Rust rewrite with ratatui** (`MIGRATION_RUST.md`, deleted): the
  496-line core is a bounded, reviewable change; a UI rewrite is neither, and
  throws away a working frontend.
- **Sidecar daemon**: see decision 1.
- **Migrating the cache now**: see decision 2.
