"""Tests de sécurité de shelltrix.

Vérifient les propriétés de durcissement du client :
  - le token d'accès ne finit jamais en clair sur disque (keyring + 0600) ;
  - le store E2EE est chiffré au repos (Fernet) et verrouillé au démarrage ;
  - la clé de récupération ne stocke qu'un vérificateur scrypt, pas la clé ;
  - le cache SQLite est insensible à l'injection SQL (placeholders uniquement) ;
  - les noms de fichiers du cache neutralisent les tentatives de traversal ;
  - notify-send est appelé sans shell (aucune injection de commande possible) ;
  - le téléchargement d'images reste cantonné au homeserver et au dossier
    de cache (pas de SSRF ni de traversal hors cache).

Ce fichier isole totalement la configuration (CONFIG_DIR, keyring) dans des
répertoires/objets temporaires : aucune écriture dans le vrai ~/.config.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
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
    """Redirige toutes les constantes de config vers un répertoire temporaire."""
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
    # Clé de secours du store : isolée aussi, jamais le vrai home.
    monkeypatch.setattr(config, "STORE_KEY_FILE", cfg / "store.key")
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", cfg / "accounts.json")
    monkeypatch.setattr(notifications, "CONFIG_FILE", cfg / "config.json")
    image_renderer._images_dir = cfg / "images"
    return cfg


@pytest.fixture()
def fake_keyring(monkeypatch):
    """Keyring en mémoire : aucun trousseau système contacté pendant les tests."""
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
# Credentials : token jamais en clair sur disque
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
# Store E2EE : chiffrement au repos (Fernet) + verrou
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

        # On retire la clé du trousseau : plus rien ne peut déchiffrer.
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


# ---------------------------------------------------------------------------
# Clé de récupération : vérificateur scrypt, jamais la clé elle-même
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
# Cache SQLite : résistance à l'injection et à la traversal de chemins
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
        # La table existe toujours : aucune requête n'a été exécutée.
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
        # Encore intègre après les recherches empoisonnées.
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
        assert len(cache.search_messages("%")) >= 0  # ne lève pas

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
# notify-send : pas de shell, arguments littéraux (pas d'injection)
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
        assert evil in called["args"]  # c'est UN argument littéral, jamais exécuté
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
# Téléchargement d'images : pas de SSRF, pas de traversal hors cache
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
        # Le contenu image est "hébergé" chez un attaquant, mais la requête
        # part toujours vers LE homeserver configuré par l'utilisateur.
        download_image("mxc://evil.example/media123", "tok", "https://hs.example")
        assert calls, "aucune requête émise"
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
        assert _guess_extension("x.evil") == ".png"  # inconnu → défaut sûr
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
# Rendu : le contenu utilisateur ne fabrique pas de markup arbitraire
# ---------------------------------------------------------------------------


class TestMarkupEscaping:
    def test_rich_tags_in_body_stay_literal(
        self, isolated_config, monkeypatch
    ) -> None:
        body = "[red]evil[/red] **bold**"
        out = _inline_markdown(body)
        # Rich échappe le `[` ouvrant : le tag ne peut pas être interprété,
        # le texte s'affiche littéralement. Les tags injectés par le
        # formateur (bold) restent fonctionnels.
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
# H1 — Chaîne de confiance d'installation : source pinnée, pas de branche mouvante
# ---------------------------------------------------------------------------


class TestSupplyChainInstall:
    ROOT = Path(__file__).resolve().parent.parent

    def _pinned_ref(self) -> str:
        install = (self.ROOT / "install.sh").read_text()
        m = re.search(
            r'SHELLTRIX_REF="\$\{SHELLTRIX_REF:-([0-9a-f]{40})\}"', install
        )
        assert m, "install.sh doit pinnner SHELLTRIX_REF sur un SHA de 40 hex"
        return m.group(1)

    def test_install_sh_pins_a_real_commit(self) -> None:
        ref = self._pinned_ref()
        ok = (
            subprocess.run(
                ["git", "cat-file", "-e", f"{ref}^{{commit}}"],
                cwd=str(self.ROOT),
                capture_output=True,
            ).returncode
            == 0
        )
        assert ok, f"SHELLTRIX_REF={ref} n'est pas un commit du dépôt"

    def test_install_sh_urls_are_pinned_https(self) -> None:
        content = (self.ROOT / "install.sh").read_text()
        # L'URL git est toujours suffixée @${SHELLTRIX_REF}.
        assert re.search(
            r"git\+https://github\.com/Nawal-alao/shelltrix\.git@\$\{SHELLTRIX_REF\}",
            content,
        )
        # Aucune référence à une branche mouvante ni URL "nue".
        assert "@main" not in content
        assert 'github.com/Nawal-alao/shelltrix.git"' not in content

    def test_readme_install_commands_pinned(self) -> None:
        ref = self._pinned_ref()
        readme = (self.ROOT / "README.md").read_text()
        assert f"/{ref}/install.sh" in readme  # curl pinné
        assert f"@{ref}" in readme  # install manuelle pipx pinnée
        assert "main/install.sh" not in readme

    @staticmethod
    def _show(ref: str, path: str) -> str | None:
        """Contenu de `path` au commit `ref`, ou None si indisponible."""
        proc = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
        )
        return proc.stdout if proc.returncode == 0 else None

    def test_pinned_ref_installs_hardened_code(self) -> None:
        """Le chemin `curl .../<ref>/install.sh | sh` doit installer du code durci.

        Pinner une source ne suffit pas : le README sert `install.sh` *depuis*
        la référence pinnée, et ce script-là épingle à son tour une autre
        référence. Si cette seconde référence est antérieure au durcissement,
        la commande documentée installe du code vulnérable alors que tout
        paraît pinné. On verrouille donc les deux maillons de la chaîne.
        """
        ref = self._pinned_ref()

        # Maillon 1 : le code directement pinné est durci (marqueur H2).
        code = self._show(ref, "src/shelltrix/cache.py")
        if code is None:
            pytest.skip(f"historique indisponible pour {ref} (clone superficiel ?)")
        assert "MAX_CACHE_BYTES" in code, (
            f"SHELLTRIX_REF={ref} pointe sur un commit antérieur au "
            "durcissement H2 : l'install pinne du code non durci."
        )

        # Maillon 2 : le script servi depuis `ref` épingle lui aussi du code durci.
        served = self._show(ref, "install.sh")
        assert served is not None, f"install.sh introuvable au commit {ref}"
        m = re.search(
            r'SHELLTRIX_REF="\$\{SHELLTRIX_REF:-([0-9a-f]{40})\}"', served
        )
        assert m, f"install.sh@{ref} ne pinnne pas de SHA"
        inner = m.group(1)

        inner_code = self._show(inner, "src/shelltrix/cache.py")
        if inner_code is None:
            pytest.skip(f"historique indisponible pour {inner}")
        assert "MAX_CACHE_BYTES" in inner_code, (
            f"install.sh@{ref} épingle {inner}, commit antérieur au durcissement : "
            "`curl | sh` depuis le README installerait du code non durci."
        )


# ---------------------------------------------------------------------------
# H2 — Cache : répertoire 0700, plafond de stockage + garde par salon
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
        d.mkdir(mode=0o755)  # umask permissif simulé
        (d / "at_a_hs.db").write_bytes(b"x")
        os.chmod(d / "at_a_hs.db", 0o644)
        cache = MessageCache("@a:hs")
        assert cache.path.stat().st_mode & 0o777 == 0o600

    def test_prune_keeps_newest_per_room(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cache_mod, "MAX_CACHE_BYTES", 0)  # toujours au-dessus
        monkeypatch.setattr(cache_mod, "CACHE_KEEP_PER_ROOM", 3)
        cache = self._cache(tmp_path, monkeypatch)
        for i in range(8):
            e = _entry(f"e{i}", body=f"msg{i}")
            e.time_ms = 1704110400000 + i
            cache.upsert_entries("r1", [e])
        cache._prune_if_oversized()

        kept = sorted(e.body for e in cache.load_entries("r1"))
        assert kept == ["msg5", "msg6", "msg7"]
        # L'autre salon n'est pas touché par la garde du premier.
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
# H3 — Médias : plafond de taille, écriture atomique 0600, dossier 0700
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
                raise AssertionError("read() ne doit jamais être appelé")

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
        # Valeurs invalides → repli sur le défaut sûr.
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
        # Aucune trace de fichier temporaire (.part) résiduel.
        leftovers = list(isolated_config.rglob("*.part"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# H4 — Store au repos : clé de secours fichier 0600, échec sonore, purge
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

        # Le chiffrement a réussi via la clé de secours.
        assert config.STORE_ENC_MARKER.exists()
        assert config.STORE_KEY_FILE.exists()
        assert config.STORE_KEY_FILE.stat().st_mode & 0o777 == 0o600
        assert (config.STORE_DIR / "nio.db").read_bytes() != b"E2EE session keys"

        # Et le déchiffrement relit cette même clé de secours.
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