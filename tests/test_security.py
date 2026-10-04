"""shelltrix security tests.

Verify the hardening properties of the client:
  - the access token never lands in plaintext on disk (keyring + 0600);
  - the E2EE store is encrypted at rest (Fernet) and locked on startup;
  - the recovery key only stores an scrypt verifier, not the key itself;
  - the SQLite cache is immune to SQL injection (placeholders only);
  - the cache file names neutralize traversal attempts;
  - notify-send is called without a shell (no command injection possible);
  - image download stays confined to the homeserver and the cache directory
    (no SSRF and no traversal outside the cache).

This file fully isolates the configuration (CONFIG_DIR, keyring) into
temporary directories/objects: nothing is written to the real ~/.config.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import shelltrix.cache as cache_mod
import shelltrix.config as config
from shelltrix.cache import MessageCache
from shelltrix.config import StoreEncryptionError, StoreLockedError
from shelltrix.formatting import (
    TimelineEntry,
    _inline_markdown,
    highlight_mentions,
)
from shelltrix.image_renderer import download_image, _guess_extension
from shelltrix.notifications import notify


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Redirects all config constants to a temporary directory."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    import shelltrix.accounts as accounts
    import shelltrix.image_renderer as image_renderer
    import shelltrix.notifications as notifications

    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "CREDENTIALS_FILE", cfg / "credentials.json")
    monkeypatch.setattr(config, "STORE_DIR", cfg / "store")
    monkeypatch.setattr(
        config, "STORE_ENC_MARKER", cfg / "store" / ".shelltrix-encrypted"
    )
    monkeypatch.setattr(config, "RECOVERY_FILE", cfg / "recovery.json")
    # Store fallback key: also isolated, never the real home.
    monkeypatch.setattr(config, "STORE_KEY_FILE", cfg / "store.key")
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", cfg / "accounts.json")
    monkeypatch.setattr(notifications, "CONFIG_FILE", cfg / "config.json")
    image_renderer._images_dir = cfg / "images"
    return cfg


def _entry(event_id: str, body: str = "hello", sender: str = "@bob:hs") -> TimelineEntry:
    return TimelineEntry(
        sender=sender,
        display_name="Bob",
        is_own=False,
        time_ms=1704110400000,
        body=body,
        event_id=event_id,
    )


# ---------------------------------------------------------------------------
# Credentials: token never in plaintext on disk
# ---------------------------------------------------------------------------


class TestCredentialStorage:
    def test_token_not_written_to_credentials_file(
        self, isolated_config, fake_keyring
    ) -> None:
        creds = config.Credentials(
            "https://hs", "@alice:hs", "DEVICE", "supersecret-token"
        )
        creds.save()

        raw = config.CREDENTIALS_FILE.read_text()
        assert "supersecret-token" not in raw
        assert "access_token" not in raw
        for field in ("homeserver", "user_id", "device_id"):
            assert field in raw

    def test_token_stored_in_keyring(
        self, isolated_config, fake_keyring
    ) -> None:
        creds = config.Credentials(
            "https://hs", "@alice:hs", "DEVICE", "supersecret-token"
        )
        creds.save()
        assert (
            fake_keyring[(config.KEYRING_SERVICE, "@alice:hs:access_token")]
            == "supersecret-token"
        )

    def test_credentials_file_permissions_0600(
        self, isolated_config, fake_keyring
    ) -> None:
        config.Credentials("https://hs", "@a:hs", "D", "tok").save()
        mode = config.CREDENTIALS_FILE.stat().st_mode & 0o777
        assert mode == 0o600

    def test_load_restores_token_from_keyring(
        self, isolated_config, fake_keyring
    ) -> None:
        config.Credentials("https://hs", "@a:hs", "D", "tok").save()
        loaded = config.Credentials.load()
        assert loaded is not None
        assert loaded.access_token == "tok"

    def test_accounts_file_never_contains_token(
        self, isolated_config, fake_keyring
    ) -> None:
        from shelltrix.accounts import get_manager

        creds = config.Credentials("https://hs", "@a:hs", "D", "topsecret")
        creds.save()
        get_manager().add(creds)
        raw = config.CONFIG_DIR.joinpath("accounts.json").read_text()
        assert "topsecret" not in raw


# ---------------------------------------------------------------------------
# E2EE store: encryption at rest (Fernet) + lock
# ---------------------------------------------------------------------------


class TestStoreEncryption:
    def _make_store_with_data(self) -> None:
        config.STORE_DIR.mkdir(parents=True, exist_ok=True)
        (config.STORE_DIR / "nio.db").write_bytes(b"E2EE session keys")

    def test_encrypt_obfuscates_then_decrypt_roundtrip(
        self, isolated_config, fake_keyring
    ) -> None:
        self._make_store_with_data()
        db = config.STORE_DIR / "nio.db"

        config.encrypt_store()
        assert config.STORE_ENC_MARKER.exists()
        encrypted = db.read_bytes()
        assert encrypted != b"E2EE session keys"
        assert b"E2EE session keys" not in encrypted

        config.decrypt_store()
        assert not config.STORE_ENC_MARKER.exists()
        assert db.read_bytes() == b"E2EE session keys"

    def test_decrypt_without_key_raises_store_locked(
        self, isolated_config, fake_keyring
    ) -> None:
        self._make_store_with_data()
        config.encrypt_store()
        assert config.STORE_ENC_MARKER.exists()

        # We remove the key from the keyring: nothing can decrypt any more.
        store_keys = [
            k for k in fake_keyring if k[1] == config._USERNAME_STORE_KEY
        ]
        for k in store_keys:
            del fake_keyring[k]

        with pytest.raises(StoreLockedError):
            config.decrypt_store()

    def test_recovery_key_unlocks_store(
        self, isolated_config, fake_keyring
    ) -> None:
        self._make_store_with_data()
        config.encrypt_store()

        secret = config.reveal_recovery_secret()
        store_keys = [
            k for k in fake_keyring if k[1] == config._USERNAME_STORE_KEY
        ]
        for k in store_keys:
            del fake_keyring[k]

        config.decrypt_store(recovery_key=secret)
        assert not config.STORE_ENC_MARKER.exists()
        assert (config.STORE_DIR / "nio.db").read_bytes() == b"E2EE session keys"


def test_store_is_isolated_from_the_real_config(tmp_path) -> None:
    """`ShelltrixClient.stop()` calls `encrypt_store()`, so a test that merely
    stops a client would otherwise rewrite the user's real store."""
    assert config.STORE_DIR.is_relative_to(tmp_path)
    assert config.STORE_ENC_MARKER.is_relative_to(tmp_path)
    assert config.STORE_KEY_FILE.is_relative_to(tmp_path)


# ---------------------------------------------------------------------------
# Store encryption: never stack a new Fernet layer on an encrypted file
# ---------------------------------------------------------------------------

# A real nio store starts with the SQLite magic. The encryption tests below
# use it so that a store file is recognizable as such.
SQLITE_HEADER = b"SQLite format 3\x00"


class TestStoreReEncryption:
    """`encrypt_store()` runs on every exit while `decrypt_store()` is skipped
    (the `olm is None` guard in `load_local_store()` returns before it), so a
    launch/exit cycle used to wrap the store in one more Fernet layer: +33% per
    run, 33 runs later a 120 KiB store had become 1.5 GiB."""

    def _plain_store(self, name: str = "nio.db", size: int = 4096) -> Path:
        config.STORE_DIR.mkdir(parents=True, exist_ok=True)
        payload = bytearray(SQLITE_HEADER)
        payload += bytes(range(256)) * (size // 256)
        path = config.STORE_DIR / name
        path.write_bytes(bytes(payload[:size]))
        return path

    def test_five_cycles_keep_the_size_constant(self, isolated_config, fake_keyring):
        db = self._plain_store()
        original = db.read_bytes()

        sizes = []
        for _ in range(5):
            # No decrypt_store(): load_local_store() returns before reaching it.
            config.encrypt_store()
            sizes.append(db.stat().st_size)

        assert len(set(sizes)) == 1, f"store size changed between cycles: {sizes}"

        # Exactly one layer: a single decrypt gives the original plaintext back.
        fernet = config._store_fernet(create=False)
        assert fernet is not None
        assert fernet.decrypt(db.read_bytes()) == original

    def test_mixed_directory_encrypts_only_the_plaintext_file(
        self, isolated_config, fake_keyring
    ):
        config.STORE_DIR.mkdir(parents=True, exist_ok=True)
        plain = self._plain_store("plain.db")
        original = plain.read_bytes()

        # A file already carrying a Fernet layer, as after a previous exit.
        fernet = config._store_fernet()
        already = config.STORE_DIR / "already.db"
        already.write_bytes(fernet.encrypt(b"payload" + SQLITE_HEADER))
        already_before = already.read_bytes()

        config.encrypt_store()

        assert already.read_bytes() == already_before, "re-encrypted a token"
        assert fernet.decrypt(plain.read_bytes()) == original

    def test_non_sqlite_plaintext_is_encrypted_and_a_token_is_left_alone(
        self, isolated_config, fake_keyring
    ):
        config.STORE_DIR.mkdir(parents=True, exist_ok=True)
        opaque = config.STORE_DIR / "opaque.bin"
        opaque.write_bytes(b"E2EE session keys")
        fernet = config._store_fernet()
        token = config.STORE_DIR / "token.db"
        token.write_bytes(fernet.encrypt(b"payload"))
        token_before = token.read_bytes()

        config.encrypt_store()

        # Not a SQLite file, no base64url header: encrypted all the same.
        assert fernet.decrypt(opaque.read_bytes()) == b"E2EE session keys"
        assert token.read_bytes() == token_before

    def test_new_plaintext_file_is_still_encrypted_after_the_marker_exists(
        self, isolated_config, fake_keyring
    ):
        first = self._plain_store("first.db")
        first_original = first.read_bytes()
        config.encrypt_store()
        assert config.STORE_ENC_MARKER.exists()
        first_size = first.stat().st_size

        second = self._plain_store("second.db")
        second_original = second.read_bytes()

        config.encrypt_store()

        fernet = config._store_fernet(create=False)
        assert fernet.decrypt(second.read_bytes()) == second_original
        assert fernet.decrypt(first.read_bytes()) == first_original
        assert first.stat().st_size == first_size, "first file was wrapped twice"


# ---------------------------------------------------------------------------
# Store hygiene: leftover temp files and empty files are never store data
# ---------------------------------------------------------------------------


class TestStoreTmpHygiene:
    """An interrupted rotation leaves a `.shelltrix-tmp-*` file behind. It is
    not store data: encrypting it grew the store, decrypting it raised
    `StoreLockedError` (Fernet.decrypt(b"") -> InvalidToken)."""

    def test_encrypt_store_ignores_tmp_and_empty_files(
        self, isolated_config, fake_keyring
    ):
        config.STORE_DIR.mkdir(parents=True, exist_ok=True)
        db = config.STORE_DIR / "nio.db"
        db.write_bytes(SQLITE_HEADER)
        stale = config.STORE_DIR / ".shelltrix-tmp-abc123"
        stale.write_bytes(b"")
        empty = config.STORE_DIR / "empty.db"
        empty.write_bytes(b"")

        config.encrypt_store()

        assert stale.read_bytes() == b""
        assert empty.read_bytes() == b""

    def test_decrypt_store_ignores_tmp_and_empty_files(
        self, isolated_config, fake_keyring
    ):
        config.STORE_DIR.mkdir(parents=True, exist_ok=True)
        db = config.STORE_DIR / "nio.db"
        db.write_bytes(SQLITE_HEADER)
        config.encrypt_store()
        (config.STORE_DIR / ".shelltrix-tmp-abc123").write_bytes(b"")
        (config.STORE_DIR / "empty.db").write_bytes(b"")

        # Used to raise StoreLockedError("wrong key or corrupted store").
        config.decrypt_store()

        assert not config.STORE_ENC_MARKER.exists()
        assert db.read_bytes() == SQLITE_HEADER

    def test_stale_tmp_files_are_removed_at_startup(
        self, isolated_config, fake_keyring
    ):
        config.STORE_DIR.mkdir(parents=True, exist_ok=True)
        stale = config.STORE_DIR / ".shelltrix-tmp-abc123"
        stale.write_bytes(b"")
        old = time.time() - config.STORE_TMP_MAX_AGE - 60
        os.utime(stale, (old, old))
        keep = config.STORE_DIR / "nio.db"
        keep.write_bytes(SQLITE_HEADER)

        config.ensure_store_dir()

        assert not stale.exists()
        assert keep.exists()

    def test_a_fresh_tmp_file_survives_startup(
        self, isolated_config, fake_keyring
    ):
        """Another instance may be mid-rotation: its scratch file is younger
        than an hour and must not be deleted."""
        config.STORE_DIR.mkdir(parents=True, exist_ok=True)
        fresh = config.STORE_DIR / ".shelltrix-tmp-inprogress"
        fresh.write_bytes(b"")

        config.ensure_store_dir()

        assert fresh.exists()


# ---------------------------------------------------------------------------
# Recovery key: scrypt verifier, never the key itself
# ---------------------------------------------------------------------------


class TestRecoveryKey:
    def test_verifier_file_does_not_contain_the_key(
        self, isolated_config, fake_keyring
    ) -> None:
        secret = "dGVzdGtleQ=="
        config.recovery_save(secret)
        data = json.loads(config.RECOVERY_FILE.read_text())
        assert set(data) == {"salt", "hash"}
        assert secret not in config.RECOVERY_FILE.read_text()
        assert len(bytes.fromhex(data["salt"])) == 16
        assert len(bytes.fromhex(data["hash"])) == 32  # dklen scrypt

    def test_verify_accepts_only_exact_key(self, isolated_config) -> None:
        config.recovery_save("dGVzdGtleQ==")
        assert config.recovery_verify("dGVzdGtleQ==") is True
        assert config.recovery_verify("AAAAAAAAAA==") is False

    def test_recovery_file_permissions_0600(self, isolated_config) -> None:
        config.recovery_save("dGVzdGtleQ==")
        assert config.RECOVERY_FILE.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# SQLite cache: resistance to injection and to path traversal
# ---------------------------------------------------------------------------


class TestCacheSecurity:
    def test_sql_injection_in_room_id_is_harmless(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr("shelltrix.cache.CONFIG_DIR", tmp_path)
        cache = MessageCache("@a:hs")
        evil_room = "room'); DROP TABLE messages;--"
        cache.upsert_entries(evil_room, [_entry("e1", body="injected")])
        cache.upsert_entries("safe_room", [_entry("e2", body="legit")])

        assert len(cache.load_entries(evil_room)) == 1
        assert len(cache.load_entries("safe_room")) == 1
        # The table still exists: no query was executed.
        table = cache._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        assert table is not None

    def test_sql_injection_in_search_query_is_harmless(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr("shelltrix.cache.CONFIG_DIR", tmp_path)
        cache = MessageCache("@a:hs")
        cache.upsert_entries("r1", [_entry("e1", body="normal message")])

        poison = "x'); DROP TABLE messages;--"
        assert cache.search_messages(poison) == []
        assert cache.search_messages("' OR '1'='1") == []
        # Still intact after the poisoned searches.
        table = cache._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        assert table is not None

    def test_like_wildcards_cannot_pull_extra_rows(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr("shelltrix.cache.CONFIG_DIR", tmp_path)
        cache = MessageCache("@a:hs")
        cache.upsert_entries("r1", [_entry("e1", body="alpha-static")])
        assert len(cache.search_messages("alpha")) == 1
        assert len(cache.search_messages("%")) >= 0  # does not raise

    def test_cache_filename_neutralizes_path_traversal(self, tmp_path) -> None:
        evil_user = "@alice:../../../../tmp/pwned"
        cache = MessageCache(evil_user, cache_dir=tmp_path)
        assert cache.path.parent == tmp_path

    def test_cache_filename_stays_in_cache_dir(self, tmp_path) -> None:
        cache = MessageCache("@alice:hs;rm -rf /", cache_dir=tmp_path)
        assert cache.path.parent == tmp_path

    def test_cache_database_permissions_0600(self, tmp_path) -> None:
        cache = MessageCache("@a:hs", cache_dir=tmp_path)
        assert cache.path.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# notify-send: no shell, literal arguments (no injection)
# ---------------------------------------------------------------------------


class TestNotifyInjection:
    def test_popen_args_are_literal_and_no_shell(
        self, isolated_config, monkeypatch
    ) -> None:
        called: dict = {}

        def fake_which(name: str) -> str:
            return "/usr/bin/notify-send"

        def fake_popen(args, **kwargs):
            called["args"] = list(args)
            called["shell"] = kwargs.get("shell")
            return object()

        monkeypatch.setattr(shutil, "which", fake_which)
        monkeypatch.setattr("shelltrix.notifications.subprocess.Popen", fake_popen)

        evil = "$(rm -rf /); `id`; | whoami; && reboot"
        notify("Room; rm -rf", "attacker", evil)

        assert called["shell"] is None or called["shell"] is False
        assert called["args"][0] == "notify-send"
        assert "--app-name=shelltrix" in called["args"]
        assert evil in called["args"]  # it is ONE literal argument, never run
        assert "rm -rf" in called["args"][-1]

    def test_no_op_when_notify_send_missing(
        self, isolated_config, monkeypatch
    ) -> None:
        called: dict = {}

        def fake_popen(*args, **kwargs):
            called["called"] = True
            return object()

        monkeypatch.setattr(shutil, "which", lambda *a: None)
        monkeypatch.setattr("shelltrix.notifications.subprocess.Popen", fake_popen)

        notify("room", "sender", "body")
        assert "called" not in called


# ---------------------------------------------------------------------------
# Image download: no SSRF, no traversal outside the cache
# ---------------------------------------------------------------------------


class TestImageDownloadSecurity:
    def test_rejects_non_mxc_schemes_without_request(
        self, isolated_config, monkeypatch
    ) -> None:
        calls: list[str] = []

        def fake_request(req, timeout=30):
            calls.append(req.full_url)
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(urllib.request, "urlopen", fake_request)
        for bad in (
            "file:///etc/passwd",
            "http://evil.example/x",
            "gopher://evil.example/1_",
        ):
            assert download_image(bad, "tok", "https://hs.example") is None
        assert calls == []

    def test_request_stays_on_homeserver_host(
        self, isolated_config, monkeypatch
    ) -> None:
        calls: list[str] = []

        def fake_request(req, timeout=30):
            calls.append(req.full_url)
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(urllib.request, "urlopen", fake_request)
        # The image content is "hosted" by an attacker, but the request
        # still goes to THE homeserver configured by the user.
        download_image("mxc://evil.example/media123", "tok", "https://hs.example")
        assert calls, "no request issued"
        assert calls[0].startswith("https://hs.example/_matrix/media/r0/download/")

    def test_media_suffix_cannot_redirect_away(
        self, isolated_config, monkeypatch
    ) -> None:
        calls: list[str] = []

        def fake_request(req, timeout=30):
            calls.append(req.full_url)
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(urllib.request, "urlopen", fake_request)
        download_image("mxc://hs.example/@evil:matrix.org/pwn", "tok", "http://hs.example")
        assert calls[0].startswith("http://hs.example/_matrix/media/r0/download/")

    def test_cache_path_never_escapes_images_dir(
        self, isolated_config, monkeypatch
    ) -> None:
        class FakeResp:
            headers = {}
            state = {"sent": False}

            def read(self, size: int = -1) -> bytes:
                if not self.state["sent"]:
                    self.state["sent"] = True
                    return b"IMAGEBYTES"
                return b""  # EOF

            def __enter__(self):
                return self

            def __exit__(self, *a) -> bool:
                return False

        monkeypatch.setattr(
            urllib.request, "urlopen", lambda req, timeout=30: FakeResp()
        )
        p = download_image(
            "mxc://hs.example/../../../../../../../etc/passwd",
            "tok",
            "https://hs.example",
        )
        assert p is not None
        assert p.parent == isolated_config / "images"

    def test_guess_extension_is_an_allowlist(self) -> None:
        assert _guess_extension("x.png") == ".png"
        assert _guess_extension("a.b.webp") == ".webp"
        assert _guess_extension("x.evil") == ".png"  # unknown → safe default
        assert _guess_extension("x.php") == ".png"
        assert "/" not in _guess_extension("../../../../x.png")

    def test_download_uses_bearer_auth_header(
        self, isolated_config, monkeypatch
    ) -> None:
        seen: dict = {}

        def fake_request(req, timeout=30):
            seen["auth"] = req.get_header("Authorization")
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(urllib.request, "urlopen", fake_request)
        download_image("mxc://hs.example/m", "tok123", "https://hs.example")
        assert seen["auth"] == "Bearer tok123"


# ---------------------------------------------------------------------------
# Rendering: user content cannot forge arbitrary markup
# ---------------------------------------------------------------------------


class TestMarkupEscaping:
    def test_rich_tags_in_body_stay_literal(
        self, isolated_config, monkeypatch
    ) -> None:
        body = "[red]evil[/red] **bold**"
        out = _inline_markdown(body)
        # Rich escapes the opening `[`: the tag cannot be interpreted,
        # the text displays literally. The tags injected by the
        # formatter (bold) remain functional.
        assert r"\[red]" in out
        assert r"\[/red]" in out
        assert "[bold]" in out

    def test_highlight_mentions_no_partial_match(self) -> None:
        out = highlight_mentions("hello @bob2 and @bob", "@bob:matrix.org")
        assert out.count("[bold]") == 1

    def test_highlight_mentions_full_match(self) -> None:
        out = highlight_mentions("hi @bob:matrix.org", "@bob:matrix.org")
        assert out.count("[bold]") == 1


# ---------------------------------------------------------------------------
# H1 — Install trust chain: pinned source, no moving branch
# ---------------------------------------------------------------------------


class TestSupplyChainInstall:
    ROOT = Path(__file__).resolve().parent.parent

    # A pinned ref is either a SHA (40 hex) or the release tag.
    # The tag is necessary because a file cannot contain the SHA of the
    # commit that contains it: `v1.0.0` does designate the last commit,
    # whereas a SHA written in that same commit would necessarily lag by one.
    _REF_RE = r'([0-9a-f]{40}|v[0-9]+(?:\.[0-9]+)*(?:-[0-9A-Za-z.]+)?)'
    _PIN_RE = r'SHELLTRIX_REF="\$\{SHELLTRIX_REF:-' + _REF_RE + r'\}"'

    def _pinned_ref(self) -> str:
        install = (self.ROOT / "install.sh").read_text()
        m = re.search(self._PIN_RE, install)
        assert m, (
            "install.sh must pin SHELLTRIX_REF to a 40-hex SHA "
            "or to the release tag (e.g. v1.0.0)"
        )
        return m.group(1)

    @staticmethod
    def _resolve(ref: str) -> str | None:
        """SHA of the commit designated by `ref`, or None if absent from the clone."""
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip() or None

    def test_install_sh_pins_a_real_commit(self) -> None:
        ref = self._pinned_ref()
        if self._resolve(ref) is None:
            pytest.skip(f"{ref} absent from the clone (clone without tags?)")
        assert not ref.startswith("main"), "a moving branch is not pinned"

    def test_install_sh_urls_are_pinned_https(self) -> None:
        content = (self.ROOT / "install.sh").read_text()
        # The git URL is always suffixed with @${SHELLTRIX_REF}.
        assert re.search(
            r"git\+https://github\.com/Nawal-alao/shelltrix\.git@\$\{SHELLTRIX_REF\}",
            content,
        )
        # No reference to a moving branch and no "bare" URL.
        assert "@main" not in content
        assert 'github.com/Nawal-alao/shelltrix.git"' not in content

    def test_readme_install_commands_pinned(self) -> None:
        ref = self._pinned_ref()
        readme = (self.ROOT / "README.md").read_text()
        assert f"/{ref}/install.sh" in readme  # pinned curl
        assert f"@{ref}" in readme  # pinned manual pipx install
        assert "main/install.sh" not in readme

    @staticmethod
    def _check_ref(ref: str) -> subprocess.CompletedProcess[str]:
        """Run install.sh's own ref validation, in isolation.

        `--check-ref` exists so this exercises the shipped validation instead
        of a copy of it that could drift from the script.
        """
        return subprocess.run(
            ["sh", "install.sh", "--check-ref", ref],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
        )

    def test_installer_accepts_its_own_pinned_ref(self) -> None:
        """`install.sh` must accept the ref it pins itself.

        Regression: the released v1.0.1 installer validated SHELLTRIX_REF as
        hexadecimal only, so its own default `v1.0.1` was rejected and
        `curl … | sh` died at step 5 — after having installed libolm and
        pipx. Every other check in this class passed, because they all read
        the pin as a string instead of asking the script whether it is legal.
        """
        ref = self._pinned_ref()
        proc = self._check_ref(ref)
        assert proc.returncode == 0, (
            f"install.sh pins {ref} but rejects it as invalid: `curl | sh` "
            f"would abort before installing. Output: {proc.stdout}{proc.stderr}"
        )

    def test_installer_rejects_unsafe_refs(self) -> None:
        """The ref reaches a git URL: keep the shell out of it.

        Rejected are the empty string (silently installs HEAD), anything
        starting with `-` (read as an option rather than a ref), and anything
        carrying shell metacharacters or whitespace, since the ref is passed
        into `uv tool install "git+…@$REF"`.
        """
        for bad in ("", "-rf", "--version", "v1.0.1;id", "v1 0 1", "$(id)",
                    "v1.0.1|sh", "v1.0.1&&id", "../../etc/passwd", "v1.0.1\nid"):
            proc = self._check_ref(bad)
            assert proc.returncode != 0, (
                f"install.sh accepts the unsafe ref {bad!r}; it must be refused"
            )

    def test_installer_rejects_bad_ref_before_touching_the_system(self) -> None:
        """An invalid ref must abort before libolm or pipx is installed.

        The check used to live at step 5, so a typo'd ref installed system
        packages first and failed afterwards — leaving the machine modified by
        an install that never happened.
        """
        install = (self.ROOT / "install.sh").read_text()
        assert 'if ! valid_ref "$SHELLTRIX_REF"' in install, (
            "install.sh no longer validates the ref with valid_ref; if the "
            "check moved or was renamed, re-check where it runs relative to "
            "the side effects below"
        )
        check = install.index('if ! valid_ref "$SHELLTRIX_REF"')
        for side_effect in ("apt-get install", "dnf install", "pacman -S",
                            "brew install libolm"):
            assert install.index(side_effect) > check, (
                f"{side_effect} runs before the ref is validated: a bad "
                "SHELLTRIX_REF would modify the system before failing"
            )

    def test_pinned_ref_matches_the_declared_version(self) -> None:
        """The pin and the package version must designate the same release.

        Nothing else links `install.sh` to `pyproject.toml`: the other checks
        in this class only compare the installer with the README. A release
        cut with a forgotten pin therefore passes every test, then quietly
        installs the *previous* tag. Bumping the version has to move the pin.
        """
        root = self.ROOT
        m = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M)
        assert m, "no `version = \"...\"` found in pyproject.toml"
        version = m.group(1)
        assert self._pinned_ref() == f"v{version}", (
            f"install.sh pins {self._pinned_ref()} but pyproject.toml declares "
            f"{version}: the installer would serve another release. "
            "Run ./bump.sh to move both."
        )
        init = (root / "src" / "shelltrix" / "__init__.py").read_text()
        assert f'__version__ = "{version}"' in init, (
            f'__init__.__version__ does not match pyproject.toml ({version})'
        )

    @staticmethod
    def _show(ref: str, path: str) -> str | None:
        """Content of `path` at commit `ref`, or None if unavailable."""
        proc = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
        )
        return proc.stdout if proc.returncode == 0 else None

    def test_pinned_ref_installs_hardened_code(self) -> None:
        """The `curl .../<ref>/install.sh | sh` path must install hardened code.

        Pinning a source is not enough: the README serves `install.sh` *from*
        the pinned ref, and that script in turn pins yet another ref. If that
        second ref predates the hardening, the documented command installs
        vulnerable code while everything looks pinned. So we lock down both
        links of the chain.
        """
        ref = self._pinned_ref()

        # Link 1: the directly pinned code is hardened (H2 marker).
        code = self._show(ref, "src/shelltrix/cache.py")
        if code is None:
            pytest.skip(f"history unavailable for {ref} (shallow clone?)")
        assert "MAX_CACHE_BYTES" in code, (
            f"SHELLTRIX_REF={ref} points at a commit predating the H2 "
            "hardening: the install pins unhardened code."
        )

        # Link 2: the script served from `ref` also pins hardened code.
        served = self._show(ref, "install.sh")
        assert served is not None, f"install.sh not found at commit {ref}"
        m = re.search(self._PIN_RE, served)
        assert m, f"install.sh@{ref} pins neither a SHA nor a tag"
        inner = m.group(1)

        inner_code = self._show(inner, "src/shelltrix/cache.py")
        if inner_code is None:
            pytest.skip(f"history unavailable for {inner}")
        assert "MAX_CACHE_BYTES" in inner_code, (
            f"install.sh@{ref} pins {inner}, a commit predating the hardening: "
            "`curl | sh` from the README would install unhardened code."
        )

    def test_pinned_ref_is_in_this_history(self) -> None:
        """The pinned ref must belong to `main`'s history.

        It is set on the release commit, hence necessarily an ancestor of
        `HEAD` — and not a commit from a diverged branch or from another
        repository, which `pipx install git+…@ref` would accept without
        protest. (The "is the pin up to date?" question is verified at
        release time: the tag is set on the last commit, and `install.sh` is
        committed in the same batch.)
        """
        ref = self._pinned_ref()
        if self._resolve(ref) is None:
            pytest.skip(f"{ref} absent from the clone (tag not pushed?)")
        merged = subprocess.run(
            ["git", "merge-base", "--is-ancestor", f"{ref}^{{commit}}", "HEAD"],
            cwd=str(self.ROOT),
            capture_output=True,
        )
        assert merged.returncode == 0, (
            f"SHELLTRIX_REF={ref} is not an ancestor of HEAD: "
            "the installer would serve code from another history."
        )


# ---------------------------------------------------------------------------
# H2 — Cache: 0700 directory, storage cap + per-room guard
# ---------------------------------------------------------------------------


class TestCacheStorageGuards:
    def _cache(self, tmp_path, monkeypatch) -> MessageCache:
        monkeypatch.setattr("shelltrix.cache.CONFIG_DIR", tmp_path)
        return MessageCache("@a:hs")

    def test_cache_dir_permissions_0700(self, tmp_path, monkeypatch) -> None:
        self._cache(tmp_path, monkeypatch)
        mode = (tmp_path / "cache").stat().st_mode & 0o777
        assert mode == 0o700

    def test_database_restored_to_0600_when_preexisting(
        self, tmp_path, monkeypatch
    ) -> None:
        d = tmp_path / "cache"
        d.mkdir(mode=0o755)  # simulated permissive umask
        (d / "at_a_hs.db").write_bytes(b"x")
        os.chmod(d / "at_a_hs.db", 0o644)
        cache = MessageCache("@a:hs")
        assert cache.path.stat().st_mode & 0o777 == 0o600

    def test_prune_keeps_newest_per_room(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cache_mod, "MAX_CACHE_BYTES", 0)  # always over the cap
        monkeypatch.setattr(cache_mod, "CACHE_KEEP_PER_ROOM", 3)
        cache = self._cache(tmp_path, monkeypatch)
        for i in range(8):
            e = _entry(f"e{i}", body=f"msg{i}")
            e.time_ms = 1704110400000 + i
            cache.upsert_entries("r1", [e])
        cache._prune_if_oversized()

        kept = sorted(e.body for e in cache.load_entries("r1"))
        assert kept == ["msg5", "msg6", "msg7"]
        # The other room is not touched by the first room's guard.
        other = _entry("x1", body="other")
        other.time_ms = 1704110400000
        cache.upsert_entries("r2", [other])
        cache._prune_if_oversized()
        assert [e.body for e in cache.load_entries("r2")] == ["other"]

    def test_prune_is_noop_under_cap(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cache_mod, "MAX_CACHE_BYTES", 10**12)
        cache = self._cache(tmp_path, monkeypatch)
        for i in range(10):
            cache.upsert_entries("r1", [_entry(f"e{i}", body=f"msg{i}")])
        assert len(cache.load_entries("r1")) == 10


# ---------------------------------------------------------------------------
# H3 — Media: size cap, atomic 0600 write, 0700 directory
# ---------------------------------------------------------------------------


class TestImageDownloadLimits:
    def _fake_resp(self, payload: bytes) -> object:
        state = {"sent": False}

        class FakeResp:
            headers = {}

            def read(self, size: int = -1) -> bytes:
                if not state["sent"]:
                    state["sent"] = True
                    return payload
                return b""  # EOF

            def __enter__(self):
                return self

            def __exit__(self, *a) -> bool:
                return False

        return FakeResp()

    def test_content_length_over_cap_refuses_without_read(
        self, isolated_config, monkeypatch
    ) -> None:
        import shelltrix.image_renderer as ir

        monkeypatch.setattr(ir, "DEFAULT_MAX_IMAGE_BYTES", 16)

        class FakeResp:
            headers = {"Content-Length": "999999999"}

            def read(self, size: int = -1) -> bytes:
                raise AssertionError("read() must never be called")

            def __enter__(self):
                return self

            def __exit__(self, *a) -> bool:
                return False

        monkeypatch.setattr(
            urllib.request, "urlopen", lambda req, timeout=30: FakeResp()
        )
        assert (
            download_image("mxc://hs.example/big", "tok", "https://hs.example")
            is None
        )

    def test_stream_exceeding_cap_is_refused(self, isolated_config, monkeypatch) -> None:
        import shelltrix.image_renderer as ir

        monkeypatch.setattr(ir, "DEFAULT_MAX_IMAGE_BYTES", 5)
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda req, timeout=30: self._fake_resp(b"0123456789"),
        )
        assert (
            download_image("mxc://hs.example/big", "tok", "https://hs.example")
            is None
        )

    def test_max_image_bytes_from_config(self, isolated_config) -> None:
        import shelltrix.image_renderer as ir

        config.CONFIG_DIR.joinpath("config.json").write_text(
            json.dumps({"max_image_bytes": 1234})
        )
        assert ir._max_image_bytes() == 1234
        # Invalid values → fall back to the safe default.
        config.CONFIG_DIR.joinpath("config.json").write_text(
            json.dumps({"max_image_bytes": -7})
        )
        assert ir._max_image_bytes() == ir.DEFAULT_MAX_IMAGE_BYTES
        config.CONFIG_DIR.joinpath("config.json").write_text("{not json")
        assert ir._max_image_bytes() == ir.DEFAULT_MAX_IMAGE_BYTES

    def test_downloaded_file_and_dir_permissions(
        self, isolated_config, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda req, timeout=30: self._fake_resp(b"IMAGEBYTES"),
        )
        p = download_image("mxc://hs.example/im", "tok", "https://hs.example")
        assert p is not None
        assert p.parent == isolated_config / "images"
        assert p.parent.stat().st_mode & 0o777 == 0o700
        assert p.stat().st_mode & 0o777 == 0o600
        assert p.read_bytes() == b"IMAGEBYTES"
        # No leftover temporary file (.part) trace.
        leftovers = list(isolated_config.rglob("*.part"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# H4 — Store at rest: file fallback key 0600, loud failure, purge
# ---------------------------------------------------------------------------


class TestStoreKeyFallback:
    def _store_with_data(self) -> None:
        config.STORE_DIR.mkdir(parents=True, exist_ok=True)
        (config.STORE_DIR / "nio.db").write_bytes(b"E2EE session keys")

    def test_fallback_keyfile_0600_without_keyring(self, isolated_config, monkeypatch) -> None:
        import keyring

        def fail_set(service, username, password):
            raise keyring.errors.PasswordSetError()

        monkeypatch.setattr(keyring, "set_password", fail_set)
        monkeypatch.setattr(keyring, "get_password", lambda *a: None)

        self._store_with_data()
        config.encrypt_store()

        # Encryption succeeded via the fallback key.
        assert config.STORE_ENC_MARKER.exists()
        assert config.STORE_KEY_FILE.exists()
        assert config.STORE_KEY_FILE.stat().st_mode & 0o777 == 0o600
        assert (config.STORE_DIR / "nio.db").read_bytes() != b"E2EE session keys"

        # And decryption reads back that same fallback key.
        config.decrypt_store()
        assert not config.STORE_ENC_MARKER.exists()
        assert (config.STORE_DIR / "nio.db").read_bytes() == b"E2EE session keys"

    def test_encrypt_raises_when_no_key_can_be_persisted(
        self, isolated_config, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "_set_store_key", lambda key: False)
        self._store_with_data()
        with pytest.raises(StoreEncryptionError):
            config.encrypt_store()

    def test_remove_store_purges_fallback_keyfile(
        self, isolated_config, fake_keyring
    ) -> None:
        config.STORE_KEY_FILE.write_text("AAAA")
        config.STORE_KEY_FILE.chmod(0o600)
        self._store_with_data()
        config.remove_store()
        assert not config.STORE_KEY_FILE.exists()
        assert not config.STORE_DIR.exists()
        assert not config.RECOVERY_FILE.exists()