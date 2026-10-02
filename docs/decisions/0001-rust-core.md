# ADR 0001 — Move the core to Rust, keep Textual

- **Status**: Accepted. Packaging resolved on 2026-10-02 (see *Resolved —
  packaging*); the network and E2EE slices are not started.
- **Date**: 2026-10-02
- **Scope**: architecture. No code written yet.

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
- The 238 tests pass unmodified. A test failure means the facade diverged, so
  the interface is verified 238 times for free, on every run.

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
| Seam (`shelltrix._core`), backend selection, parity tests | done, 260 tests |
| `/sync` parsing → timeline messages | done, 30x measured |
| Network + event loop | **not started** |
| E2EE (olm store, key management) | **not started** |
| Rust as the default backend | not started |

The seam is deliberately **not** wired into `matrix_client.py`: the client
consumes events through matrix-nio callbacks, while a matrix-sdk core emits a
stream. Bridging the two is a change of architecture, not a substitution of a
function call, and is left as one reviewed step.

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