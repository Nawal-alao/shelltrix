"""Global fixtures: isolate the SQLite cache, the olm store, the keyring.

Tests that instantiate `ChatScreen` create a `MessageCache` (whose default
directory is `~/.config/shelltrix/cache`). We redirect `CONFIG_DIR` of the
`cache` module to a temporary directory so that tests never write into the
user's real configuration directory.

The olm store gets the same treatment, for a worse reason: `ShelltrixClient.stop()`
calls `encrypt_store()`, which reads every file in the store directory and
rewrites it encrypted. Against the real store that is a multi-gigabyte read and
write per test — on one machine here it was a 1.6 GB SQLite file, so a test that
merely stops a client took minutes and gigabytes of RAM. Redirecting the store
constants keeps the suite fast and leaves the user's session keys alone.

The system keyring is replaced by an in-memory store: without this, a test
that encrypts the store drops the session E2EE key into the user's keyring,
and the following tests read it back — as a result, a test that expects
"no key available" would then depend on the keyring history of the machine.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("shelltrix.cache.CONFIG_DIR", tmp_path / "config")
    yield


@pytest.fixture(autouse=True)
def _isolate_store_dir(tmp_path, monkeypatch):
    """Points the store encryption paths at a temporary directory.

    `config.py` derives these from `CONFIG_DIR` at import time, so each one
    has to be patched rather than just the root.
    """
    from shelltrix import config

    store = tmp_path / "store"
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "STORE_DIR", store)
    monkeypatch.setattr(config, "STORE_ENC_MARKER", store / ".shelltrix-encrypted")
    monkeypatch.setattr(config, "STORE_KEY_FILE", tmp_path / "config" / "store.key")
    yield


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    """In-memory keyring, for all tests: no contact with the system."""
    import keyring

    store: dict[tuple[str, str], str] = {}

    def set_pw(service: str, username: str, password: str) -> None:
        store[(service, username)] = password

    def get_pw(service: str, username: str) -> str | None:
        return store.get((service, username))

    def del_pw(service: str, username: str) -> None:
        if (service, username) in store:
            del store[(service, username)]
            return
        raise keyring.errors.PasswordDeleteError()

    monkeypatch.setattr(keyring, "set_password", set_pw)
    monkeypatch.setattr(keyring, "get_password", get_pw)
    monkeypatch.setattr(keyring, "delete_password", del_pw)
    return store
