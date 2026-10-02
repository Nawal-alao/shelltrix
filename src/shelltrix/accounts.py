"""Multi-account management for shelltrix.

Lets you save several Matrix accounts and switch between them.
Credentials are stored in ~/.config/shelltrix/accounts.json (metadata)
and the tokens in the system keyring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List

from .config import CONFIG_DIR, KEYRING_SERVICE, Credentials

ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"


@dataclass
class AccountInfo:
    """Lightweight account information (no secrets)."""
    user_id: str
    homeserver: str
    device_id: str
    label: str  # Display name (e.g. "Alice @ matrix.org")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AccountInfo":
        return cls(**data)


class AccountManager:
    """Manages the list of saved accounts."""

    def __init__(self) -> None:
        self._accounts: List[AccountInfo] = []
        self._load()

    def _load(self) -> None:
        """Loads the account list from accounts.json."""
        if not ACCOUNTS_FILE.exists():
            self._accounts = []
            return
        try:
            data = json.loads(ACCOUNTS_FILE.read_text())
            self._accounts = [AccountInfo.from_dict(a) for a in data.get("accounts", [])]
        except (OSError, ValueError, KeyError, TypeError):
            self._accounts = []

    def _save(self) -> None:
        """Persists the account list."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {"accounts": [a.to_dict() for a in self._accounts]}
        ACCOUNTS_FILE.write_text(json.dumps(data, indent=2))
        ACCOUNTS_FILE.chmod(0o600)

    @property
    def accounts(self) -> List[AccountInfo]:
        return list(self._accounts)

    def count(self) -> int:
        return len(self._accounts)

    def get(self, user_id: str) -> AccountInfo | None:
        """Fetches an account by user_id."""
        for acc in self._accounts:
            if acc.user_id == user_id:
                return acc
        return None

    def add(self, creds: Credentials) -> AccountInfo:
        """Adds an account to the list (or replaces it if already there)."""
        # Drop the old entry if it exists
        self._accounts = [a for a in self._accounts if a.user_id != creds.user_id]
        label = _make_label(creds.user_id, creds.homeserver)
        info = AccountInfo(
            user_id=creds.user_id,
            homeserver=creds.homeserver,
            device_id=creds.device_id,
            label=label,
        )
        self._accounts.append(info)
        self._save()
        return info

    def remove(self, user_id: str) -> None:
        """Removes an account from the list (but not the creds/keyring)."""
        self._accounts = [a for a in self._accounts if a.user_id != user_id]
        self._save()

    def load_credentials(self, user_id: str) -> Credentials | None:
        """Fully loads an account's creds (with token from keyring)."""
        acc = self.get(user_id)
        if acc is None:
            return None
        return Credentials.load_for(acc.user_id, acc.homeserver, acc.device_id)


def _make_label(user_id: str, homeserver: str) -> str:
    """Builds a readable label: @alice — matrix.org."""
    # Extract the localpart of @alice:matrix.org
    short = user_id
    if ":" in user_id and user_id.startswith("@"):
        short = user_id.split(":", 1)[0]
    # Extract the domain of https://matrix.org
    domain = homeserver
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    domain = domain.rstrip("/")
    return f"{short} — {domain}"


# Global instance
_manager: AccountManager | None = None


def get_manager() -> AccountManager:
    """Returns the global account manager instance."""
    global _manager
    if _manager is None:
        _manager = AccountManager()
    return _manager
