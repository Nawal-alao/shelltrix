"""Tests for the image_renderer module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from shelltrix.image_renderer import (
    _guess_extension,
    _has_pillow,
    _rgb_hex,
    is_image_message,
    render_image,
    render_image_placeholder,
    render_image_textual,
    format_image_message,
)


class TestGuessExtension:
    """Tests for _guess_extension()."""

    def test_png_extension(self) -> None:
        assert _guess_extension("abc123.png") == ".png"

    def test_jpg_extension(self) -> None:
        assert _guess_extension("photo.jpg") == ".jpg"

    def test_jpeg_extension(self) -> None:
        assert _guess_extension("photo.jpeg") == ".jpeg"

    def test_no_extension_defaults_png(self) -> None:
        assert _guess_extension("abcdef123456") == ".png"

    def test_unknown_extension_defaults_png(self) -> None:
        assert _guess_extension("file.xyz") == ".png"


class TestIsImageMessage:
    """Tests for is_image_message()."""

    def test_detects_marker(self) -> None:
        assert is_image_message("__SHELLTRIX_IMAGE__:/path/to/img.png") is True

    def test_rejects_plain_text(self) -> None:
        assert is_image_message("hello world") is False

    def test_rejects_empty(self) -> None:
        assert is_image_message("") is False


class TestRenderImagePlaceholder:
    """Tests for render_image_placeholder()."""

    def test_includes_filename(self) -> None:
        result = render_image_placeholder("photo.png")
        assert "photo.png" in result

    def test_includes_camera_icon(self) -> None:
        result = render_image_placeholder("photo.png")
        assert "📷" in result

    def test_returns_string(self) -> None:
        result = render_image_placeholder("test.jpg")
        assert isinstance(result, str)


class TestRgbHex:
    """Tests for _rgb_hex()."""

    def test_black(self) -> None:
        assert _rgb_hex(0, 0, 0) == "#000000"

    def test_white(self) -> None:
        assert _rgb_hex(255, 255, 255) == "#ffffff"

    def test_red(self) -> None:
        assert _rgb_hex(255, 0, 0) == "#ff0000"


class TestHasPillow:
    """Tests for _has_pillow()."""

    def test_false_when_pillow_missing(self) -> None:
        with patch.dict("sys.modules", {"PIL": None}):
            assert _has_pillow() is False

    def test_true_when_pillow_available(self) -> None:
        with patch("builtins.__import__", side_effect=__import__):
            assert isinstance(_has_pillow(), bool)


class TestRenderImageTextual:
    """Tests for render_image_textual()."""

    def test_none_when_pillow_missing(self) -> None:
        with patch("shelltrix.image_renderer._has_pillow", return_value=False):
            assert render_image_textual("/tmp/whatever.png") is None

    def test_none_when_file_missing(self) -> None:
        with patch("shelltrix.image_renderer._has_pillow", return_value=True):
            assert render_image_textual("/tmp/does-not-exist.png") is None


class TestRenderImage:
    """Tests for render_image()."""

    def test_returns_string_always(self) -> None:
        result = render_image("/tmp/missing.png")
        assert isinstance(result, str)

    def test_falls_back_to_placeholder(self) -> None:
        with patch("shelltrix.image_renderer.render_image_textual", return_value=None):
            result = render_image("/tmp/missing.png")
            assert "missing.png" in result


class TestRenderImageTextualReal:
    """Tests for half-block rendering with a real image (requires Pillow)."""

    @staticmethod
    def _skip_if_no_pillow() -> None:
        try:
            import PIL  # noqa: F401
        except Exception:
            import pytest

            pytest.skip("Pillow not installed")

    def test_renders_half_blocks_for_small_image(self) -> None:
        self._skip_if_no_pillow()
        from PIL import Image

        import tempfile
        import os

        img = Image.new("RGB", (4, 4))
        pix = img.load()
        for y in range(4):
            for x in range(4):
                pix[x, y] = (255, 0, 0)
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        img.save(path)
        try:
            out = render_image_textual(path, max_width_cols=4)
            assert out is not None
            lines = out.split("\n")
            assert len(lines) == 2  # 4 rows → 2 lines of half-blocks
            assert all("▀" in line for line in lines)
            assert all("on" in line for line in lines)
        finally:
            os.unlink(path)

    def test_returns_none_on_corrupt_file(self) -> None:
        self._skip_if_no_pillow()

        import tempfile
        import os

        fd, path = tempfile.mkstemp(suffix=".png")
        os.write(fd, b"not an image")
        os.close(fd)
        try:
            assert render_image_textual(path, max_width_cols=4) is None
        finally:
            os.unlink(path)


class TestFormatImageMessage:
    """Tests for format_image_message()."""

    def test_returns_placeholder_when_download_fails(self) -> None:
        with patch("shelltrix.image_renderer.download_image", return_value=None):
            result = format_image_message(
                "Alice",
                "mxc://server/abc123",
                "photo.png",
                "token",
                "https://matrix.org",
            )
            assert "[dim]" in result
            assert "photo.png" in result

    def test_returns_marker_when_download_succeeds(self) -> None:
        with patch(
            "shelltrix.image_renderer.download_image", return_value=Path("/tmp/img.png")
        ):
            result = format_image_message(
                "Alice",
                "mxc://server/abc123",
                "photo.png",
                "token",
                "https://matrix.org",
            )
            assert result.startswith("__SHELLTRIX_IMAGE__:")
