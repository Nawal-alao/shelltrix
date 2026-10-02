"""Fixtures globales : isole le cache SQLite et le trousseau système.

Les tests qui instancient `ChatScreen` créent un `MessageCache` (dont le
répertoire par défaut est `~/.config/shelltrix/cache`). On redirige `CONFIG_DIR`
du module `cache` vers un répertoire temporaire pour que les tests n'écrivent
jamais dans le vrai répertoire de configuration de l'utilisateur.

Le trousseau système est remplacé par un store en mémoire : sans cela, un
test qui chiffre le store y dépose la clé E2EE de session dans le keyring de
l'utilisateur, et les tests suivants la relisent — résultat, un test qui
attend « aucune clé disponible » dépend alors de l'historique du keyring de
la machine.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("shelltrix.cache.CONFIG_DIR", tmp_path / "config")
    yield


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    """Keyring en mémoire, pour tous les tests : aucun contact au système."""
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
