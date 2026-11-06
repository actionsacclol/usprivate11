"""API-key storage + request signing for the Polymarket US exchange.

Polymarket US is a CFTC-regulated exchange with a plain account: no wallet, no
chain, nothing signed per order. Every authenticated request carries an Ed25519
signature of `timestamp + METHOD + path` (see `auth_headers`). Keys are created
at polymarket.us/developer after identity verification in the Polymarket US app.

Krypt PolyBot US stores the key at:

    <userData>/credentials/apikey.mainnet.key     {keyId, secretKey} JSON

The path to <userData> is provided via the KRYPT_POLYBOT_US_USERDATA env var
(set by the Electron main process on backend spawn). In dev / standalone mode it
falls back to `./credentials/` next to this file. The file is encrypted at rest:
on Windows with the user account key (DPAPI); on macOS/Linux with a random
master key held in the OS keychain. If no backend is usable it falls back to
plaintext-0600 and warns.

The `env` argument is kept on the public functions for call-site compatibility
with the rest of the bot; it is always "mainnet".
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

NETWORK = "mainnet"

_DPAPI_MARKER = b"#KRYPT-DPAPI-v1\n"
_KEYRING_MARKER = b"#KRYPT-KEYRING-v1\n"
_KEYRING_SERVICE = "krypt-polybot-us"
_KEYRING_MASTER_ACCOUNT = "cred-master-key"
_warned_plaintext = False
_plaintext_error_logged = False   # gate the ERROR log to once per PROCESS (startup)
_keyring_master_cache: Optional[bytes] = None


def _dpapi_available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(data: bytes) -> "_DATA_BLOB":
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _from_blob(blob: "_DATA_BLOB") -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob.pbData)

    def _dpapi_encrypt(data: bytes) -> bytes:
        out = _DATA_BLOB()
        blob_in = _to_blob(data)
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(out)
        ):
            raise OSError("CryptProtectData failed")
        return _from_blob(out)

    def _dpapi_decrypt(data: bytes) -> bytes:
        out = _DATA_BLOB()
        blob_in = _to_blob(data)
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(out)
        ):
            raise OSError("CryptUnprotectData failed")
        return _from_blob(out)
else:  # pragma: no cover - non-Windows fallback
    def _dpapi_encrypt(data: bytes) -> bytes:
        raise OSError("DPAPI not available")

    def _dpapi_decrypt(data: bytes) -> bytes:
        raise OSError("DPAPI not available")


# ── OS keychain (macOS Keychain / Linux Secret Service via `keyring`) ──
# These wrappers swallow every failure (no backend, locked keychain, no
# D-Bus, keyring not bundled) and signal it by returning None/False, so a
# missing keychain degrades to plaintext rather than breaking credential I/O.


def _keyring_set(account: str, value: str) -> bool:
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, account, value)
        return True
    except Exception:
        return False


def _keyring_get(account: str) -> Optional[str]:
    try:
        import keyring
        return keyring.get_password(_KEYRING_SERVICE, account)
    except Exception:
        return None


def _fernet(key: bytes):
    from cryptography.fernet import Fernet
    return Fernet(key)


def _get_master_key() -> Optional[bytes]:
    """Fetch (or, on first use, create) the Fernet master key from the OS
    keychain. Returns None when no keychain backend is usable — the caller then
    falls back to plaintext. The key never touches the credential directory."""
    global _keyring_master_cache
    if _keyring_master_cache is not None:
        return _keyring_master_cache
    existing = _keyring_get(_KEYRING_MASTER_ACCOUNT)
    if existing:
        key = existing.encode("ascii")
        try:
            _fernet(key)  # validate it's a usable Fernet key before trusting it
            _keyring_master_cache = key
            return key
        except Exception:
            logger.warning("Stored keychain master key is corrupt; regenerating.")
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    if not _keyring_set(_KEYRING_MASTER_ACCOUNT, key.decode("ascii")):
        return None
    # Read-back guard: a no-op backend can accept set() yet return None on get();
    # in that case treat the keychain as unusable rather than write ciphertext we
    # could never decrypt.
    if _keyring_get(_KEYRING_MASTER_ACCOUNT) != key.decode("ascii"):
        return None
    _keyring_master_cache = key
    return key


def _keyring_available() -> bool:
    return _get_master_key() is not None


def _keyring_encrypt(data: bytes) -> bytes:
    key = _get_master_key()
    if key is None:
        raise OSError("no OS keychain backend available")
    return _fernet(key).encrypt(data)


def _keyring_decrypt(token: bytes) -> bytes:
    key = _get_master_key()
    if key is None:
        raise OSError("no OS keychain backend available")
    return _fernet(key).decrypt(token)


def _chmod_600(path: Path) -> None:
    """Best-effort owner-only perms on POSIX (no-op / not meaningful on Windows,
    where DPAPI + NTFS ACLs already scope the file to the user)."""
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _atomic_write_600(path: Path, tmp: Path, data: bytes) -> None:
    """Write `data` to `tmp` as an OWNER-ONLY file (0600 from CREATION on POSIX),
    then atomically replace `path`. Creating the temp file 0600 up front means the
    (plaintext-fallback) key is never briefly world-readable — not in the pre-chmod
    window, and no 0644 crash-residue if the process dies before the replace."""
    try:
        if tmp.exists():
            tmp.unlink()   # clear any stale / loose-perm residue from a prior crash
    except OSError:
        pass
    if os.name == "posix":
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    else:
        tmp.write_bytes(data)  # Windows: DPAPI scopes content + NTFS ACL scopes the file
    _chmod_600(tmp)
    tmp.replace(path)


def _write_secret_bytes(path: Path, data: bytes) -> None:
    """Atomically write `data`, encrypted with the best available backend
    (DPAPI → OS keychain → plaintext). Every branch writes via a 0600-from-creation
    temp file so even the plaintext fallback key is never world-readable at rest."""
    global _warned_plaintext, _plaintext_error_logged
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path.parent, 0o700)  # credentials/ owner-only (not umask ~0755)
        except OSError:
            pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    if _dpapi_available():
        try:
            _atomic_write_600(path, tmp, _DPAPI_MARKER + base64.b64encode(_dpapi_encrypt(data)))
            return
        except Exception as e:
            logger.warning(f"DPAPI encrypt failed, trying OS keychain: {e}")
    try:
        if _keyring_available():
            _atomic_write_600(path, tmp, _KEYRING_MARKER + _keyring_encrypt(data))
            return
    except Exception as e:
        logger.warning(f"Keychain encrypt failed, storing plaintext: {e}")
    if not _warned_plaintext:
        logger.error(
            "SECURITY: credentials stored UNENCRYPTED (no DPAPI or OS-keychain "
            "backend on this platform). The file is chmod 600 but NOT encrypted "
            "at rest. Surfaced to the UI as credentials_status().keyStoredUnencrypted."
        )
        _warned_plaintext = True
    # A fresh plaintext write may flip the on-disk state, so re-arm the
    # once-per-process startup ERROR to reflect it on the next status poll.
    _plaintext_error_logged = False
    _atomic_write_600(path, tmp, data)


def _read_secret_bytes(path: Path, upgrade: bool = True) -> bytes:
    """Read a credential file, decrypting DPAPI- or keychain-wrapped content.
    Legacy plaintext is returned as-is and (when a backend is available)
    re-encrypted in place on this read."""
    raw = path.read_bytes()
    if raw.startswith(_DPAPI_MARKER):
        return _dpapi_decrypt(base64.b64decode(raw[len(_DPAPI_MARKER):]))
    if raw.startswith(_KEYRING_MARKER):
        return _keyring_decrypt(raw[len(_KEYRING_MARKER):])
    # Legacy / plaintext: hand back the bytes, and opportunistically upgrade the
    # file to the best at-rest scheme so it isn't left in the clear.
    if upgrade and (_dpapi_available() or _keyring_available()):
        try:
            _write_secret_bytes(path, raw)
        except Exception:
            pass
    return raw


def _is_plaintext_secret_file(path: Path) -> bool:
    """True iff `path` holds a secret written WITHOUT at-rest encryption (neither
    the DPAPI nor the keychain marker prefix). Derives the state from the file
    itself, so it's durable across restarts and can't go stale like a saved flag."""
    try:
        if not path.exists():
            return False
        with open(path, "rb") as fh:
            head = fh.read(max(len(_DPAPI_MARKER), len(_KEYRING_MARKER)))
        return not (head.startswith(_DPAPI_MARKER) or head.startswith(_KEYRING_MARKER))
    except OSError:
        return False


def key_stored_unencrypted(env: Optional[str] = None) -> bool:
    """Durable, queryable flag: is the wallet private key sitting on disk in
    CLEARTEXT (plaintext-0600 fallback, because no DPAPI/keychain backend was
    usable)? Surfaced in `credentials_status()` so the UI can warn the user."""
    try:
        return _is_plaintext_secret_file(_wallet_key_file(env or _current_env))
    except Exception:
        return False


def _log_plaintext_at_rest_once() -> None:
    """Log at ERROR (once per process = once per startup) when the wallet key is
    stored unencrypted. Loud + repeated-every-launch, unlike the buried one-time
    warning at write time that the user never sees."""
    global _plaintext_error_logged
    if _plaintext_error_logged:
        return
    try:
        if key_stored_unencrypted():
            _plaintext_error_logged = True
            logger.error(
                "SECURITY: wallet private key is stored UNENCRYPTED on disk "
                "(no DPAPI or OS-keychain backend available on this platform). "
                "The file is chmod 600 but NOT encrypted at rest. Anyone who can "
                "read this account's files can steal the key. "
                "credentials_status().keyStoredUnencrypted == true."
            )
    except Exception:
        pass


def _credentials_dir() -> Path:
    base = os.environ.get("KRYPT_POLYBOT_US_USERDATA")
    if base:
        return Path(base) / "credentials"
    return Path(__file__).resolve().parent / "credentials"


# Single-account model: 'mainnet' is the only env. `env` is interpolated into
# the credential FILE NAME, so an RPC-supplied value containing path separators
# could otherwise steer key writes outside the credentials dir.
_VALID_ENVS = ("mainnet",)


def _validate_env(env: str) -> str:
    e = env if isinstance(env, str) else ""
    e = e.strip()
    if (e not in _VALID_ENVS) or ("/" in e) or ("\\" in e) or (".." in e) or (os.sep in e):
        raise ValueError(
            f"invalid network env {env!r}: only {_VALID_ENVS} are allowed "
            f"(no path separators or '..')"
        )
    return e


def _wallet_key_file(env: str = NETWORK) -> Path:
    """The encrypted {keyId, secretKey} file. (Name kept: the at-rest helpers
    above and the status poll address "the key file" through it.)"""
    return _credentials_dir() / f"apikey.{_validate_env(env)}.key"


# ───────── module state ───────────────────

_current_env: str = NETWORK
_cached_creds: Optional[dict] = None
_time_offset: float = 0.0


def set_env(env: str) -> None:
    # Single-account model; accept the value for call-site compatibility.
    global _current_env
    _current_env = NETWORK


def get_env() -> str:
    return _current_env


def reset_credential_cache() -> None:
    """Drop the cached key. Call after the user saves new keys."""
    global _cached_creds
    _cached_creds = None


# ───────── API key ──────────────────────────


class CredentialError(ValueError):
    """The pasted key id / secret can't be a Polymarket US API key."""


_KEY_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _decode_secret(secret_key: str) -> bytes:
    """The 32-byte Ed25519 seed from the base64 secret the developer page
    issues. The docs' example and the official SDK both accept a 64-byte
    keypair form, whose first 32 bytes are the seed."""
    try:
        raw = base64.b64decode((secret_key or "").strip(), validate=True)
    except (binascii.Error, ValueError) as e:
        raise CredentialError("The secret key is not valid base64.") from e
    if len(raw) == 64:
        raw = raw[:32]
    if len(raw) != 32:
        raise CredentialError(
            f"The secret key decodes to {len(raw)} bytes; a Polymarket US key is 32 or 64.")
    return raw


def _load_creds() -> Optional[dict]:
    global _cached_creds
    if _cached_creds is not None:
        return _cached_creds
    f = _wallet_key_file(_current_env)
    if not f.exists():
        return None
    try:
        data = json.loads(_read_secret_bytes(f).decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — a corrupt file reads as "no key", loudly
        logger.error("could not read the API key file: %s", type(e).__name__)
        return None
    if not (isinstance(data, dict) and data.get("keyId") and data.get("secretKey")):
        return None
    _cached_creds = {"keyId": str(data["keyId"]), "secretKey": str(data["secretKey"])}
    return _cached_creds


def get_key_id() -> str:
    c = _load_creds()
    return c["keyId"] if c else ""


def get_address() -> str:
    """This account's identity for the rest of the app (instance lock, run
    ledgers, logs): the API key id. There is no wallet on this exchange."""
    key = get_key_id()
    if not key:
        raise RuntimeError("No Polymarket US API key is saved.")
    return key


def trading_address(env: Optional[str] = None) -> str:
    return get_address()


def credentials_present(env: Optional[str] = None) -> bool:
    return _wallet_key_file(env or _current_env).exists()


def _preview(key_id: str) -> str:
    return (key_id[:8] + "…" + key_id[-4:]) if len(key_id) > 12 else key_id


def credentials_status(env: Optional[str] = None) -> dict:
    """Diagnostic info for the UI. Never returns secret material."""
    e = env or _current_env
    wf = _wallet_key_file(e)
    _log_plaintext_at_rest_once()
    key_id = ""
    if wf.exists() and e == _current_env:
        key_id = get_key_id()
    return {
        "env": e,
        "hasWalletKey": wf.exists(),
        "hasApiCreds": wf.exists(),
        "address": key_id,
        "addressPreview": _preview(key_id),
        "keyId": key_id,
        "keyStoredUnencrypted": _is_plaintext_secret_file(wf),
    }


def credentials_status_all() -> dict:
    return {"current": _current_env, "mainnet": credentials_status(NETWORK)}


def save_credentials(key_id: str, secret_key: str, env: Optional[str] = None) -> None:
    """Validate and store a key. Raises CredentialError before touching disk."""
    kid = (key_id or "").strip()
    secret = (secret_key or "").strip()
    if not _KEY_ID_RE.match(kid):
        raise CredentialError("The Key ID should be a UUID, as shown on polymarket.us/developer.")
    _decode_secret(secret)
    d = _credentials_dir()
    d.mkdir(parents=True, exist_ok=True)
    _write_secret_bytes(_wallet_key_file(env or _current_env),
                        json.dumps({"keyId": kid, "secretKey": secret}).encode("utf-8"))
    reset_credential_cache()


def clear_credentials(env: Optional[str] = None) -> None:
    f = _wallet_key_file(env or _current_env)
    if f.exists():
        f.unlink()
    reset_credential_cache()


def migrate_legacy_credentials(target_env: str) -> bool:
    return False


# ───────── request signing ──────────────────


def set_time_offset(offset: float) -> None:
    """Seconds to add to the local clock (server minus local)."""
    global _time_offset
    _time_offset = float(offset or 0.0)


def get_time_offset() -> float:
    return _time_offset


def sign_message(secret_key: str, message: str) -> str:
    """Base64 Ed25519 signature of `message`. Pure."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(_decode_secret(secret_key))
    return base64.b64encode(key.sign(message.encode("utf-8"))).decode("ascii")


def auth_headers(method: str, path: str, *, now_ms: Optional[int] = None,
                 creds: Optional[dict] = None) -> dict:
    """X-PM-* headers. Signed string: `timestamp + METHOD + path`, no
    separators and no body, and never the query string: the official SDK signs
    the bare path and sends the query separately. The server allows 30 seconds of
    clock skew."""
    path = path.split("?", 1)[0]
    c = creds or _load_creds()
    if not c:
        raise RuntimeError("No Polymarket US API key is saved.")
    ts = str(now_ms if now_ms is not None else int((time.time() + _time_offset) * 1000))
    return {
        "X-PM-Access-Key": c["keyId"],
        "X-PM-Timestamp": ts,
        "X-PM-Signature": sign_message(c["secretKey"], f"{ts}{method.upper()}{path}"),
    }


_SIGNER_VECTOR_SEED = base64.b64encode(bytes(range(32))).decode("ascii")


def verify_signer() -> str:
    """Offline selftest: signing must produce a signature the matching public
    key verifies, over exactly the documented message."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    creds = {"keyId": "00000000-0000-0000-0000-000000000000", "secretKey": _SIGNER_VECTOR_SEED}
    h = auth_headers("get", "/v1/portfolio/positions", now_ms=1700000000000, creds=creds)
    pub = Ed25519PrivateKey.from_private_bytes(bytes(range(32))).public_key()
    pub.verify(base64.b64decode(h["X-PM-Signature"]),
               b"1700000000000GET/v1/portfolio/positions")
    return "ed25519 ok"


def prime_credentials(sync_time: bool = True) -> bool:
    """Load the key eagerly so a bad file surfaces at startup."""
    return _load_creds() is not None
