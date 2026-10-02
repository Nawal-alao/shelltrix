"""Tests for the config module — pure functions."""

from __future__ import annotations

import pytest

from shelltrix.config import (
    first_run_done,
    mark_first_run_done,
    normalize_recovery_key,
)


class TestNormalizeRecoveryKey:
    """Tests for normalize_recovery_key()."""

    def test_removes_spaces(self) -> None:
        result = normalize_recovery_key("abc def ghi")
        assert " " not in result

    def test_removes_newlines(self) -> None:
        result = normalize_recovery_key("abc\ndef\nghi")
        assert "\n" not in result

    def test_adds_padding(self) -> None:
        # Base64 without padding
        result = normalize_recovery_key("abc")
        # Should add padding
        assert result.endswith("=")

    def test_preserves_valid_base64(self) -> None:
        # Valid base64 with padding
        result = normalize_recovery_key("dGVzdA==")
        assert result == "dGVzdA=="

    def test_handles_empty(self) -> None:
        result = normalize_recovery_key("")
        assert result == ""


class TestFirstRunMarker:
    """Tests for the marker that shows the splash only once."""

    def test_absent_marker_means_first_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shelltrix.config.FIRST_RUN_FILE", tmp_path / ".first_run_done")
        assert first_run_done() is False

    def test_marker_written_once_splash_seen(self, tmp_path, monkeypatch):
        marker = tmp_path / ".first_run_done"
        monkeypatch.setattr("shelltrix.config.FIRST_RUN_FILE", marker)
        mark_first_run_done()
        assert marker.exists()
        assert first_run_done() is True

    def test_mark_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shelltrix.config.FIRST_RUN_FILE", tmp_path / ".first_run_done")
        mark_first_run_done()
        mark_first_run_done()
        assert first_run_done() is True

    def test_creates_config_dir(self, tmp_path, monkeypatch):
        marker = tmp_path / "shelltrix" / ".first_run_done"
        monkeypatch.setattr("shelltrix.config.FIRST_RUN_FILE", marker)
        monkeypatch.setattr("shelltrix.config.CONFIG_DIR", marker.parent)
        mark_first_run_done()
        assert first_run_done() is True
