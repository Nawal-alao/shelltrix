"""Inline image rendering in shelltrix.

Displays m.image messages in a way totally safe for Textual: the image is
decomposed into Unicode half-blocks (▀) colored in truecolor, written as plain
text lines in the RichLog — no raw escape sequence on stdout that would
corrupt the full-screen display.

This rendering works on any terminal supporting truecolor (24-bit), which
includes the vast majority of modern terminals.

Fallback: shows an [image: name] placeholder if image decoding (Pillow) is
not available.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path

from . import config

_images_dir = config.CONFIG_DIR / "images"

# Default cap on a downloaded media file (10 MB). Overridable via the
# `max_image_bytes` key of ~/.config/shelltrix/config.json. Beyond that:
# the download is refused (protection against a hostile homeserver/room
# that would fill the client's RAM and disk). The image NEVER loads more
# than `limit` bytes in memory.
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Chunked read size: bounds the buffer growth.
_READ_CHUNK = 64 * 1024


def _max_image_bytes() -> int:
    try:
        data = json.loads(config.CONFIG_DIR.joinpath("config.json").read_text())
        value = int(data.get("max_image_bytes", DEFAULT_MAX_IMAGE_BYTES))
        return value if value > 0 else DEFAULT_MAX_IMAGE_BYTES
    except (OSError, ValueError, TypeError):
        return DEFAULT_MAX_IMAGE_BYTES


def _ensure_images_dir() -> Path:
    """Media cache directory at 0700: the images (private room contents,
    possibly unencrypted) are not readable by other local accounts."""
    _images_dir.mkdir(parents=True, exist_ok=True)
    try:
        _images_dir.chmod(0o700)
    except OSError:
        pass
    return _images_dir


def download_image(mxc_url: str, access_token: str, homeserver: str) -> Path | None:
    """Downloads an image from a Matrix URL (mxc://).

    Path of the downloaded file on success, None otherwise or if the media
    exceeds the configured cap (max_image_bytes).
    """
    import urllib.request

    # Convert mxc:// to an HTTP URL
    if not mxc_url.startswith("mxc://"):
        return None
    mxc_path = mxc_url[6:]  # Strip mxc://
    base_url = homeserver.rstrip("/")
    if "://" not in base_url:
        base_url = f"https://{base_url}"
    download_url = f"{base_url}/_matrix/media/r0/download/{mxc_path}"

    # Create the cache directory (0700).
    _ensure_images_dir()

    # Filename based on the URL hash
    url_hash = base64.urlsafe_b64encode(mxc_url.encode()).decode()[:24]
    ext = _guess_extension(mxc_path)
    local_path = _images_dir / f"{url_hash}{ext}"

    # Already cached, return
    if local_path.exists() and local_path.stat().st_size > 0:
        return local_path

    limit = _max_image_bytes()
    tmp_path: Path | None = None
    try:
        req = urllib.request.Request(download_url)
        req.add_header("Authorization", f"Bearer {access_token}")
        # nosec B310 — the host of download_url is ALWAYS the user's
        # homeserver (base_url), never the image value (limited to
        # mxc:// and appended as a path suffix). No file:/ or arbitrary
        # scheme reachable: only the configured homeserver is contacted.
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            # 1) Cap announced by the server (Content-Length).
            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > limit:
                        return None
                except ValueError:
                    pass  # non-numeric header: the actual cap will suffice
            # 2) Effective cap: we never read more than `limit` bytes.
            buf = bytearray()
            while len(buf) <= limit:
                chunk = resp.read(_READ_CHUNK)
                if not chunk:
                    break
                buf.extend(chunk)
            if len(buf) > limit:
                return None
            data = bytes(buf)
        if not data:
            return None
        # 3) Atomic write at 0600 (never a readable partial file).
        fd, raw_tmp = tempfile.mkstemp(
            dir=str(_images_dir), prefix=".img-", suffix=".part"
        )
        tmp_path = Path(raw_tmp)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(raw_tmp, 0o600)
            os.replace(raw_tmp, local_path)
        except OSError:
            return None
        return local_path
    except Exception:
        return None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def _guess_extension(path: str) -> str:
    """Guesses the file extension from the MXC path."""
    if "/" in path:
        name = path.split("/")[-1]
    else:
        name = path
    # Extract the extension if present
    if "." in name:
        ext = name.rsplit(".", 1)[-1].lower()
        if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
            return f".{ext}"
    return ".png"  # Default


def render_image_placeholder(filename: str) -> str:
    """Builds a simple readable placeholder when inline rendering fails."""
    name = Path(filename).name or "image"
    return f"📷 [dim]Image · {name}[/dim]"


def _has_pillow() -> bool:
    """True if Pillow is available to decode images."""
    try:
        import PIL  # noqa: F401

        return True
    except Exception:
        return False


def _rgb_hex(r: int, g: int, b: int) -> str:
    """Converts RGB components to a Rich hexadecimal color code."""
    return f"#{r:02x}{g:02x}{b:02x}"


def render_image_textual(image_path: str | Path, max_width_cols: int = 40) -> str | None:
    """Displays an image as colored Unicode half-blocks (▀), safe for Textual.

    Each terminal cell shows 2 image rows: the top pixel is the foreground
    color, the bottom one the background color, via the upper half-block
    character ▀. The result is a plain Rich markup string, with no escape
    sequences on stdout.

    Returns None if Pillow is missing, the file is unreadable or the image
    is invalid.
    """
    if not _has_pillow():
        return None
    path = Path(image_path)
    if not path.exists():
        return None
    try:
        from PIL import Image

        img = Image.open(path).convert("RGB")
    except Exception:
        return None

    width, height = img.size
    if width <= 0 or height <= 0 or max_width_cols <= 0:
        return None

    scale = max_width_cols / width
    new_w = max(1, int(width * scale))
    new_h = max(1, int(height * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    px = img.load()

    lines = []
    for row in range(0, new_h, 2):
        cells = []
        bottom_row = min(row + 1, new_h - 1)
        for col in range(new_w):
            tr, tg, tb = px[col, row][:3]
            br, bg, bb = px[col, bottom_row][:3]
            cells.append(f"[{_rgb_hex(tr, tg, tb)} on {_rgb_hex(br, bg, bb)}]▀[/]")
        lines.append("".join(cells))
    return "\n".join(lines)


def render_image(image_path: str | Path, max_width_cols: int = 40) -> str:
    """Renders an image as TrueColor text safe for Textual.

    Always returns a string: the half-block rendering if available,
    otherwise a placeholder. No more binary output on stdout.
    """
    textual = render_image_textual(image_path, max_width_cols)
    if textual is not None:
        return textual
    return render_image_placeholder(Path(image_path).name)


def format_image_message(
    sender: str,
    image_url: str,
    filename: str,
    access_token: str,
    homeserver: str,
) -> str:
    """Formats an image message for display in RichLog.

    Caches the image download; if the download succeeds, returns the
    internal marker (as a local path to render). Otherwise, returns a
    textual placeholder.
    """
    local_path = download_image(image_url, access_token, homeserver)
    if local_path is None:
        return f"[dim]{sender}: [image: {filename}][/image][/dim]"

    return f"__SHELLTRIX_IMAGE__:{local_path}:{filename}"


def is_image_message(body: str) -> bool:
    """True if the body contains our image marker."""
    return body.startswith("__SHELLTRIX_IMAGE__:")
