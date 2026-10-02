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