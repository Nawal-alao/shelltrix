"""Desktop notifications for shelltrix.

Uses `notify-send` (Linux/BSD via libnotify) to show a notification
when a message arrives in an inactive room. Can be disabled through
the config (`notifications_enabled`, default: enabled).
"""

from __future__ import annotations

import shutil
import subprocess

from .config import CONFIG_DIR

CONFIG_FILE = CONFIG_DIR / "config.json"


def is_enabled() -> bool:
    """True if desktop notifications are enabled (default: True)."""
    try:
        import json

        data = json.loads(CONFIG_FILE.read_text())
        return bool(data.get("notifications_enabled", True))
    except (OSError, ValueError):
        return True


def notify(room_name: str, sender: str, body: str) -> None:
    """Sends a desktop notification via notify-send.

    Silent if:
    - notifications are disabled in the config
    - notify-send is not installed
    """
    if not is_enabled():
        return
    if not shutil.which("notify-send"):
        return

    body_clean = body.replace("\n", " ")[:200]
    try:
        subprocess.Popen(
            [
                "notify-send",
                "--app-name=shelltrix",
                "--category=im.received",
                f"[{room_name}] {sender}",
                body_clean,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
