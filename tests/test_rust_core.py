"""Tests for the Python/Rust core seam.

The Rust extension is optional: if it is not installed these tests skip rather
than fail, because shelltrix must work on the pure Python core alone. The
parity tests are the ones that matter — they are what makes the backend a free
choice for the user.
"""

from __future__ import annotations

import json

import pytest

from shelltrix import _core
from shelltrix._core import SyncMessage, parse_sync_messages, selected_backend

PAYLOAD = json.dumps(
    {
        "next_batch": "s1",
        "rooms": {
            "join": {
                "!a:hs": {
                    "timeline": {
                        "events": [
                            {
                                "type": "m.room.message",
                                "event_id": "$1",
                                "sender": "@alice:hs",
                                "origin_server_ts": 1_700_000_000_000,
                                "content": {"msgtype": "m.text", "body": "hello"},
                            },
                            {
                                "type": "m.room.member",
                                "event_id": "$2",
                                "sender": "@bob:hs",
                                "origin_server_ts": 1_700_000_001_000,
                                "content": {"membership": "join"},
                            },
                            {
                                "type": "m.room.message",
                                "event_id": "$3",
                                "sender": "@bob:hs",
                                "origin_server_ts": 1_700_000_002_000,
                                "content": {
                                    "msgtype": "m.image",
                                    "body": "shot.png",
                                    "m.mentions": {"user_ids": ["@alice:hs"]},
                                },
                            },
                        ]
                    }
                },
            },
            "leave": {"!x:hs": {"timeline": {"events": [
                {"type": "m.room.message", "event_id": "$9",
                 "sender": "@eve:hs", "content": {"body": "gone"}},
            ]}}},
        },
    }
).encode()

needs_rust = pytest.mark.skipif(
    not _core.rust_available(), reason="the Rust core is not installed"
)


class TestPythonBackend:
    def test_keeps_only_message_events(self) -> None:
        got = parse_sync_messages(PAYLOAD, backend="python")
        assert [m.event_id for m in got] == ["$1", "$3"]

    def test_reads_every_field(self) -> None:
        first = parse_sync_messages(PAYLOAD, backend="python")[0]
        assert first == SyncMessage(
            sender="@alice:hs",
            origin_server_ts=1_700_000_000_000,
            event_id="$1",
            msgtype="m.text",
            body="hello",
            mentions=False,
        )

    def test_detects_mentions(self) -> None:
        assert parse_sync_messages(PAYLOAD, backend="python")[1].mentions is True

    def test_skips_left_rooms(self) -> None:
        bodies = [m.body for m in parse_sync_messages(PAYLOAD, backend="python")]
        assert "gone" not in bodies

    def test_defaults_missing_fields(self) -> None:
        payload = json.dumps({"rooms": {"join": {"!a:hs": {"timeline": {"events": [
            {"type": "m.room.message", "content": {}},
        ]}}}}}).encode()
        (msg,) = parse_sync_messages(payload, backend="python")
        assert msg.msgtype == "m.text"
        assert msg.body == ""
        assert msg.sender == ""
        assert msg.origin_server_ts == 0

    def test_rejects_malformed_json(self) -> None:
        with pytest.raises(ValueError):
            parse_sync_messages(b"{not json", backend="python")

    def test_rejects_payload_without_rooms(self) -> None:
        with pytest.raises(ValueError):
            parse_sync_messages(b'{"next_batch":"s1"}', backend="python")

    def test_accepts_an_empty_room_set(self) -> None:
        assert parse_sync_messages(b'{"rooms":{"join":{}}}', backend="python") == []


@needs_rust
class TestParity:
    """The contract: the two backends must be indistinguishable."""

    @pytest.mark.parametrize(
        "payload",
        [
            PAYLOAD,
            b'{"rooms":{"join":{}}}',
            json.dumps({"rooms": {"join": {"!a:hs": {"timeline": {"events": [
                {"type": "m.room.message", "content": {}},
            ]}}}}}).encode(),
            json.dumps({"rooms": {"join": {"!a:hs": {"timeline": {"events": [
                {"type": "m.room.message", "sender": "@a:hs", "event_id": "$e",
                 "origin_server_ts": 17, "content": {"msgtype": "m.emote",
                                                     "body": "waves"}},
                {"type": "m.room.message", "sender": "@b:hs", "event_id": "$f",
                 "content": {"body": "no mention block",
                             "m.mentions": {"user_ids": []}}},
            ]}}}}}).encode(),
        ],
        ids=["typical", "empty", "bare-event", "edge-cases"],
    )
    def test_same_result_on_both_backends(self, payload: bytes) -> None:
        assert parse_sync_messages(payload, backend="python") == \
            parse_sync_messages(payload, backend="rust")

    def test_same_errors_on_both_backends(self) -> None:
        for bad in (b"{not json", b'{"next_batch":"s1"}'):
            with pytest.raises(ValueError):
                parse_sync_messages(bad, backend="python")
            with pytest.raises(ValueError):
                parse_sync_messages(bad, backend="rust")

    def test_rust_import_works(self) -> None:
        import shelltrix_core

        assert shelltrix_core.core_version()


class TestBackendSelection:
    def test_default_is_python(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SHELLTRIX_CORE", raising=False)
        assert selected_backend() == "python"

    def test_rust_when_requested_and_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHELLTRIX_CORE", "rust")
        expected = "rust" if _core.rust_available() else "python"
        assert selected_backend() == expected

    def test_unknown_value_falls_back_to_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHELLTRIX_CORE", "jaeger")
        assert selected_backend() == "python"

    def test_value_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SHELLTRIX_CORE", " RUST ")
        assert selected_backend() == ("rust" if _core.rust_available() else "python")

    def test_requesting_a_missing_core_raises_rather_than_silently_using_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit `rust` must not quietly run the Python core.

        The fallback that protects the user is in `selected_backend`. This
        explicit `parse_sync_messages(backend="rust")` is the programmer's
        choice, and silently returning Python results would hide a broken
        build behind a green test.
        """
        monkeypatch.setattr(_core, "_rust", None)
        with pytest.raises(ValueError, match="not installed"):
            parse_sync_messages(PAYLOAD, backend="rust")

@needs_rust
class TestTransportBridge:
    """The GIL, which decides whether the UI can freeze.

    matrix-sdk blocks on the network inside a call Python made. If the core
    held the GIL across that wait, every Textual widget would stop repainting
    for the duration of each `/sync` — a hang that looks like a bug in
    something else entirely. `runtime.rs` detaches the GIL around the runtime
    bridge (`py.detach` in `lib.rs`); this asserts the observable consequence,
    without needing a real homeserver.

    The threshold is half the expected tick rate, and it is loose on purpose.
    A *Python* thread that spins still drops the GIL every 5 ms, so it degrades
    to roughly 65% of the expected rate; a Rust thread that failed to detach
    would emit no interpreter check point and freeze the loop outright. Half is
    low enough to catch that, high enough not to fail on a loaded CI machine.
    """

    @staticmethod
    def _stub_server(delay: float):
        """A homeserver that stalls, so the call blocks long enough to measure.

        It deliberately never succeeds: we only need the core to be genuinely
        waiting on I/O, not to log in. That keeps the test independent of which
        endpoints matrix-sdk happens to call first.

        It answers 401 rather than 500 on purpose. matrix-sdk retries a 5xx
        with backoff — which would make this test take minutes instead of one
        second — while a 401 is a fatal auth error it surfaces immediately.
        """
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def _stall(self) -> None:
                time.sleep(delay)
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"errcode":"M_FORBIDDEN","error":"stub"}')

            do_GET = _stall
            do_POST = _stall

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_port}"

    @pytest.mark.asyncio
    async def test_the_event_loop_keeps_ticking_during_a_blocking_call(self) -> None:
        import asyncio
        import time

        delay = 1.0
        server, url = self._stub_server(delay)
        try:
            ticks = 0
            stop = False

            # Stands in for the Textual repaint loop.
            async def heartbeat() -> None:
                nonlocal ticks
                while not stop:
                    ticks += 1
                    await asyncio.sleep(0.01)

            beat = asyncio.create_task(heartbeat())
            await asyncio.sleep(0.05)
            before = ticks

            started = time.perf_counter()
            # The call is expected to fail — the stub refuses everything —
            # which is fine: the point is how long it waited, and what else
            # ran meanwhile. `wait_for` turns a regression into a failure
            # rather than a hung test run.
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(
                    asyncio.to_thread(_core.login_and_sync, url, "alice", "pw"),
                    timeout=delay * 6,
                )
            elapsed = time.perf_counter() - started

            stop = True
            await beat
            during = ticks - before
        finally:
            server.shutdown()

        assert elapsed >= delay, "the stub did not slow the call down as expected"
        expected = elapsed / 0.01
        assert during >= expected * 0.5, (
            f"the loop only ticked {during} times during a {elapsed:.2f}s blocking "
            f"call (expected about {expected:.0f}): the core is holding the GIL, "
            "so the UI would freeze"
        )

    def test_the_core_reports_a_failure_rather_than_an_empty_result(self) -> None:
        """A homeserver that refuses us must not look like a successful login."""
        server, url = self._stub_server(0)
        try:
            with pytest.raises(RuntimeError):
                _core.login_and_sync(url, "alice", "pw")
        finally:
            server.shutdown()


@needs_rust
class TestSyncStream:
    """The queue Python drains, from the Python side.

    `shelltrix-core` proves the ordering and de-duplication in Rust. What only
    Python can catch is the shape of what comes back: a transport is only
    interchangeable if it hands the UI the same values.
    """

    @staticmethod
    def _stub_server(delay: float = 0):
        """A homeserver that refuses us, so the login fails fast.

        401 rather than 500: matrix-sdk retries a 5xx with backoff, which turns
        a test into a hang.
        """
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def _refuse(self) -> None:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"errcode":"M_FORBIDDEN","error":"stub"}')

            do_GET = _refuse
            do_POST = _refuse

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_port}"

    def teardown_method(self) -> None:
        _core.stop_sync()

    def test_a_refused_login_leaves_no_stream_behind(self) -> None:
        """A failed start must not squat the slot.

        If it did, one wrong password would brick the session: every later
        start would answer "already running" and the user could never connect.
        """
        server, url = self._stub_server()
        try:
            with pytest.raises(RuntimeError):
                _core.start_sync(url, "alice", "wrong-password")
            with pytest.raises(RuntimeError, match="no sync is running"):
                _core.next_event(0)
        finally:
            server.shutdown()

    def test_asking_for_an_event_without_a_sync_says_so(self) -> None:
        """Otherwise the UI would wait on a queue that will never be filled."""
        with pytest.raises(RuntimeError, match="no sync is running"):
            _core.next_event(0)

    def test_stopping_without_a_sync_is_a_no_op(self) -> None:
        """Quit must never raise, even if the loop never started."""
        _core.stop_sync()
        _core.stop_sync()

    def test_the_facade_declares_exactly_the_core_event_fields(self) -> None:
        """Both sides must agree on the fields, or the UI silently loses data.

        A field added in Rust and forgotten in Python reads as an empty value
        rather than an error, so the mismatch has to be caught here.
        """
        import shelltrix_core

        core_fields = {a for a in dir(shelltrix_core.StreamEvent) if not a.startswith("_")}
        assert set(_core.StreamEvent.__dataclass_fields__) == core_fields
