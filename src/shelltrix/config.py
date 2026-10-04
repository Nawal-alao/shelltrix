"""Configuration and persistent credential handling for shelltrix."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import keyring
from cryptography.fernet import Fernet, InvalidToken

CONFIG_DIR = Path.home() / ".config" / "shelltrix"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
STORE_DIR = CONFIG_DIR / "store"
# scrypt verifier for the session recovery key: lets us recognize the key on a
# fresh machine without storing the key itself.
RECOVERY_FILE = CONFIG_DIR / "recovery.json"

KEYRING_SERVICE = "shelltrix"
# Keyring entry holding the access token. The token is a secret just like a
# password: we keep it in the system keyring, never in plaintext on disk.
# (nosec B105: "access_token" is a keyring account label, not a secret.)
# nosec B105
_USERNAME_ACCESS_TOKEN = "access_token"  # nosec B105

# Keyring entry holding the Fernet key that encrypts the olm store at rest.
_USERNAME_STORE_KEY = "store_key"
# Marker present only when the store is encrypted.
STORE_ENC_MARKER = STORE_DIR / ".shelltrix-encrypted"
# Fallback (file mode 0600) if the system keyring is unavailable: the store
# still ends up encrypted at rest, never leaving silent plaintext on disk.
STORE_KEY_FILE = CONFIG_DIR / "store.key"

logger = logging.getLogger(__name__)

# A nio store is a SQLite database: this is its first 16 bytes.
SQLITE_MAGIC = b"SQLite format 3\x00"
# A Fernet token is urlsafe base64 of 0x80 || timestamp(8) || iv(16) || ...
# The version byte is mandatory, so a few bytes are enough to recognize a file
# that already carries a Fernet layer -- without reading the whole file.
FTOKEN_VERSION = 0x80
FTOKEN_MAGIC_LEN = 8
B64URL_ALPHABET = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
# Prefix of the scratch file `_enc_rotate()` writes before the atomic replace.
STORE_TMP_PREFIX = ".shelltrix-tmp-"


class StoreLockedError(RuntimeError):
    """The local store is encrypted but no usable key is available:
    the user must provide their session recovery key."""


class StoreEncryptionError(RuntimeError):
    """The store CANNOT be encrypted at rest: neither the keyring nor the
    fallback file accepts the key. Raised loudly so the store is NEVER left
    in plaintext without telling the user."""


@dataclass
class Credentials:
    homeserver: str
    user_id: str
    device_id: str
    access_token: str

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    # The metadata (homeserver, user_id, device_id) is not sensitive and
    # stays in credentials.json (chmod 600). The access token, on the other
    # hand, is a secret: it lives in the system keyring, not in plaintext.

    def _keyring_username(self) -> str:
        return f"{self.user_id}:{_USERNAME_ACCESS_TOKEN}"

    @classmethod
    def load(cls) -> "Credentials | None":
        if not CREDENTIALS_FILE.exists():
            return None
        data = json.loads(CREDENTIALS_FILE.read_text())

        # 1) Token from the system keyring.
        token: str | None = None
        if data.get("user_id"):
            token = keyring.get_password(
                KEYRING_SERVICE,
                f"{data['user_id']}:{_USERNAME_ACCESS_TOKEN}",
            )

        # 2) Backward compatibility: older versions stored the token in
        #    plaintext in credentials.json. We use it as a fallback and it
        #    will be migrated to the keyring on the next save().
        legacy = data.get("access_token")
        if token is None and legacy:
            token = legacy

        return cls(
            homeserver=data["homeserver"],
            user_id=data["user_id"],
            device_id=data["device_id"],
            access_token=token or "",
        )

    @classmethod
    def load_for(cls, user_id: str, homeserver: str, device_id: str) -> "Credentials | None":
        """Loads the creds for a specific account (multi-account)."""
        token = keyring.get_password(
            KEYRING_SERVICE,
            f"{user_id}:{_USERNAME_ACCESS_TOKEN}",
        )
        return cls(
            homeserver=homeserver,
            user_id=user_id,
            device_id=device_id,
            access_token=token or "",
        )

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Non-sensitive metadata.
        CREDENTIALS_FILE.write_text(
            json.dumps(
                {
                    "homeserver": self.homeserver,
                    "user_id": self.user_id,
                    "device_id": self.device_id,
                },
                indent=2,
            )
        )
        CREDENTIALS_FILE.chmod(0o600)
        # Token in the system keyring.
        keyring.set_password(
            KEYRING_SERVICE,
            self._keyring_username(),
            self.access_token,
        )

    def remove(self) -> None:
        """Erases the persistent credentials (logout)."""
        CREDENTIALS_FILE.unlink(missing_ok=True)
        try:
            keyring.delete_password(KEYRING_SERVICE, self._keyring_username())
        except keyring.errors.PasswordDeleteError:
            pass


def ensure_store_dir() -> Path:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    remove_stale_store_tmp()
    return STORE_DIR


def skip_splash() -> bool:
    """True if the app must start without the welcome screen.

    Option read from config.json via the `skip_splash` key (default:
    disabled). Not persisted by shelltrix: the user sets it manually for a
    fast / scripted start."""
    try:
        data = json.loads(CONFIG_DIR.joinpath("config.json").read_text())
        return bool(data.get("skip_splash", False))
    except (OSError, ValueError, TypeError):
        return False


# First launch marker: the welcome splash is offered only once. Dedicated
# (empty) file rather than a config.json key, because themes.save_pref()
# rewrites config.json identically on every theme change — a marker stored
# there would be wiped on the first ctrl+t.
FIRST_RUN_FILE = CONFIG_DIR / ".first_run_done"


def first_run_done() -> bool:
    """True if the welcome splash has already been offered (first launch
    done). A missing file = first use of the machine."""
    return FIRST_RUN_FILE.exists()


def mark_first_run_done() -> None:
    """Marks the splash as offered (best effort).

    Best effort like save_pref(): a read-only filesystem must not stop the
    app from running — at worst the splash shows up again on the next
    start."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        FIRST_RUN_FILE.touch()
    except OSError:
        pass


def remove_store() -> None:
    """Deletes the local olm store (full device logout)."""
    shutil.rmtree(STORE_DIR, ignore_errors=True)
    RECOVERY_FILE.unlink(missing_ok=True)
    # The key fallback file goes away with the local purge.
    STORE_KEY_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# At-rest encryption of the olm store (E2EE encryption keys)
# ---------------------------------------------------------------------------
# nio keeps the olm/megolm session keys in plaintext in its SQLite database.
# We therefore encrypt them at rest: decrypt_store() at startup (before
# load_store()), encrypt_store() at shutdown (after close()). The Fernet key
# lives in the system keyring. The marker is only set (resp. removed) once
# every write has finished.


def _get_store_key() -> str | None:
    """Store Fernet key: keyring first, fallback file second."""
    try:
        stored = keyring.get_password(KEYRING_SERVICE, _USERNAME_STORE_KEY)
        if stored:
            return stored
    except Exception:
        pass  # keyring unavailable: we try the fallback file
    try:
        return STORE_KEY_FILE.read_text().strip() or None
    except OSError:
        return None


def _set_store_key(key: str) -> bool:
    """Persists the store key: keyring, else 0600 fallback file.

    Returns False if NO storage accepts it (the caller must then raise
    StoreEncryptionError rather than leave the store in plaintext)."""
    try:
        keyring.set_password(KEYRING_SERVICE, _USERNAME_STORE_KEY, key)
        return True
    except Exception:
        pass
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STORE_KEY_FILE.write_text(key)
        STORE_KEY_FILE.chmod(0o600)
        return True
    except OSError:
        return False


def _store_fernet(
    key: str | None = None, *, create: bool = True
) -> Fernet | None:
    """Store Fernet key: explicit key, keyring/fallback, or generation.

    - `key` given (session recovery key): we use it as is;
    - otherwise we read the persisted key (keyring, then fallback file);
    - if absent, we generate a new one only when `create` is true
      (encryption at shutdown, first creation).
    Returns None when no key exists and we do not want to create one
    (restore: the UI will ask for the session recovery key).
    Raises StoreEncryptionError if a new key cannot be persisted: we
    prefer failing loudly rather than leaving the store in plaintext."""
    if key is not None:
        return Fernet(key.encode())
    stored = _get_store_key()
    if stored:
        return Fernet(stored.encode())
    if not create:
        return None
    new_key = Fernet.generate_key()
    if not _set_store_key(new_key.decode()):
        raise StoreEncryptionError(
            "Cannot encrypt the E2EE store at rest: neither the system "
            "keyring nor the local fallback accepted the key. "
            "Aborting to avoid leaving session keys in plaintext."
        )
    return Fernet(new_key)


def _enc_store_is_encrypted() -> bool:
    return STORE_ENC_MARKER.exists()


def is_store_tmp(path: Path) -> bool:
    """True for the scratch file `_enc_rotate()` writes before replacing."""
    return path.name.startswith(STORE_TMP_PREFIX)


def _looks_like_fernet_token(head: bytes) -> bool:
    """Recognizes a Fernet token from its first bytes alone.

    The token is base64url; its very first decoded byte is the mandatory
    version byte 0x80. Decoding the 8 first characters covers it."""
    if len(head) < FTOKEN_MAGIC_LEN:
        return False
    if not B64URL_ALPHABET.issuperset(head):
        return False
    try:
        raw = base64.urlsafe_b64decode(head + b"=" * (-len(head) % 4))
    except (ValueError, binascii.Error):
        return False
    return bool(raw) and raw[0] == FTOKEN_VERSION


def store_file_kind(path: Path) -> str:
    """Classifies a store file from its first bytes: `sqlite`, `fernet` or
    `unknown`. Never reads more than a few bytes, whatever the file size."""
    try:
        with path.open("rb") as fh:
            head = fh.read(max(len(SQLITE_MAGIC), FTOKEN_MAGIC_LEN))
    except OSError as exc:
        logger.warning("store: cannot read %s (%s), left untouched", path.name, exc)
        return "unknown"
    if head.startswith(SQLITE_MAGIC):
        return "sqlite"
    if _looks_like_fernet_token(head):
        return "fernet"
    return "unknown"


def _store_candidates() -> list[Path]:
    """The actual store files: no marker, no leftover scratch file, no empty
    file. An interrupted rotation leaves a `.shelltrix-tmp-*` behind; it is
    never store data (and Fernet.decrypt(b"") raises InvalidToken)."""
    return [
        p
        for p in STORE_DIR.iterdir()
        if p.is_file()
        and p != STORE_ENC_MARKER
        and not is_store_tmp(p)
        and p.stat().st_size > 0
    ]


def remove_stale_store_tmp() -> int:
    """Deletes the scratch files left behind by an interrupted rotation, so a
    crashed exit does not make the next runs work on a growing store.
    Returns how many files were removed."""
    if not STORE_DIR.exists():
        return 0
    removed = 0
    for path in STORE_DIR.iterdir():
        if path.is_file() and is_store_tmp(path):
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("store: cannot remove %s (%s)", path.name, exc)
    if removed:
        logger.info("store: removed %d leftover temporary file(s)", removed)
    return removed


def _enc_rotate(file: Path, transform) -> None:
    """Replaces `file` with its transformed version, atomically."""
    with tempfile.NamedTemporaryFile(
        dir=str(STORE_DIR), prefix=".shelltrix-tmp-", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        tmp_path.write_bytes(transform(file.read_bytes()))
        os.replace(tmp_path, file)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# ---------------------------------------------------------------------------
# Session recovery key
# ---------------------------------------------------------------------------
# The session recovery key is the store's Fernet key (either the one from
# the keyring, or a new one generated by the user). It is presented as
# URL-safe base64 and can be typed again on another machine. We only persist
# a scrypt digest (salt + hash), never the key itself.


def normalize_recovery_key(raw: str) -> str:
    """Cleans up the input: strips spaces/newlines and re-applies the '='
    padding (URL-safe base64 without special characters)."""
    s = "".join(raw.split())
    s = s.rstrip("=")
    s += "=" * ((-len(s)) % 4)
    return s


def _recovery_digest(secret: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        secret.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32
    )


def recovery_save(secret: str) -> None:
    """Persists the verifier (scrypt) of the session recovery key so we
    can recognize it on a brand new machine."""
    normalized = normalize_recovery_key(secret)
    salt = os.urandom(16)
    data = {
        "salt": salt.hex(),
        "hash": _recovery_digest(normalized, salt).hex(),
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    RECOVERY_FILE.write_text(json.dumps(data, indent=2))
    RECOVERY_FILE.chmod(0o600)


def recovery_has_verifier() -> bool:
    return RECOVERY_FILE.exists()


def recovery_verify(secret: str) -> bool:
    """True if `secret` matches the persisted verifier."""
    try:
        data = json.loads(RECOVERY_FILE.read_text())
        salt = bytes.fromhex(data["salt"])
        expected = bytes.fromhex(data["hash"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    try:
        digest = _recovery_digest(normalize_recovery_key(secret), salt)
    except ValueError:
        return False
    return hmac.compare_digest(digest, expected)


def reveal_recovery_secret() -> str:
    """Returns the session recovery key of the active store (creates the
    keyring key if it does not exist yet) and persists its verifier."""
    key = _get_store_key()
    if key is None:
        new_key = Fernet.generate_key().decode()
        _set_store_key(new_key)  # best effort: the key stays usable in session
        key = _get_store_key() or new_key
    secret = normalize_recovery_key(key)
    recovery_save(secret)
    return secret


def regenerate_recovery_secret() -> str:
    """Generates a new store key and exposes its session recovery key.
    The old recovery key becomes invalid (the store will be re-encrypted
    with the new one at shutdown)."""
    new_key = Fernet.generate_key().decode()
    _set_store_key(new_key)  # best effort: the key stays displayable in session
    secret = normalize_recovery_key(new_key)
    recovery_save(secret)
    return secret


def decrypt_store(recovery_key: str | None = None) -> None:
    """Restores the store to plaintext if it was encrypted (called before
    load_store).

    Without a key: we use the one from the keyring (raises StoreLockedError
    if it is missing). With `recovery_key`: we accept the session recovery
    key and re-save it in the keyring on success."""
    if not _enc_store_is_encrypted():
        return
    if recovery_key is None:
        fernet = _store_fernet(create=False)
        if fernet is None:
            raise StoreLockedError(
                "The E2EE store is encrypted but its key is missing from the "
                "system keyring. Enter your recovery key to restore it."
            )
    else:
        key = normalize_recovery_key(recovery_key)
        if recovery_has_verifier() and not recovery_verify(key):
            raise StoreLockedError(
                "Invalid recovery key (does not match the saved verifier)."
            )
        try:
            fernet = _store_fernet(key=key)
        except ValueError as exc:  # malformed key (wrong length/character)
            raise StoreLockedError(
                "Invalid recovery key (bad format). Check the characters."
            ) from exc
    candidates = _store_candidates()
    try:
        for file in candidates:
            # Only a file that already carries a Fernet layer is decryptable.
            # Anything else is left alone rather than reported as corrupted.
            if store_file_kind(file) != "fernet":
                logger.warning(
                    "store: %s is not an encrypted store file, left untouched",
                    file.name,
                )
                continue
            _enc_rotate(file, fernet.decrypt)
    except InvalidToken as exc:
        raise StoreLockedError(
            "Invalid recovery key (wrong key or corrupted store)."
        ) from exc
    # Everything is decrypted: we remove the marker last.
    STORE_ENC_MARKER.unlink(missing_ok=True)
    if recovery_key is not None:
        # The key works: we re-save it for the next runs.
        _set_store_key(key)


def encrypt_store() -> None:
    """Encrypts the store (called after close(), at shutdown).

    E2EE keys protected at rest. Only sets the marker after ALL writes.
    Raises StoreEncryptionError if no key can be persisted — better to fail
    loudly than leave the store in plaintext without the user knowing.

    A file that already carries a Fernet layer is skipped: the app does not
    always decrypt at startup, and wrapping it again would add 33% to its
    size on every exit, until the exit itself fails."""
    if not STORE_DIR.exists():
        return
    fernet = _store_fernet()
    for file in _store_candidates():
        kind = store_file_kind(file)
        if kind == "sqlite":
            _enc_rotate(file, fernet.encrypt)
        elif kind == "fernet":
            logger.info("store: %s is already encrypted, left as is", file.name)
        else:
            logger.warning(
                "store: %s is not a store file, left untouched", file.name
            )
    # Everything is encrypted: we set the marker last.
    STORE_ENC_MARKER.touch()
