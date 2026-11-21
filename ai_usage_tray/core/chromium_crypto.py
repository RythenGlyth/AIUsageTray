from __future__ import annotations

import base64
import hashlib
import os
import shutil
import sqlite3
import tempfile
from typing import Optional

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

try:
    import secretstorage
except Exception:  # pragma: no cover
    secretstorage = None


def derive_chromium_key(password: bytes) -> bytes:
    """Derive 16-byte AES-128-CBC key used by Linux Chromium/Electron safeStorage."""
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1, dklen=16)


def decrypt_chromium_blob(encrypted: bytes, v11_password: Optional[bytes] = None) -> Optional[bytes]:
    """Decrypt a Chromium/Electron v10 or v11 encrypted value."""
    if not encrypted:
        return None
    prefix = encrypted[:3]
    if prefix == b"v10":
        password = b"peanuts"
        ciphertext = encrypted[3:]
    elif prefix == b"v11":
        if not v11_password:
            return None
        password = v11_password
        ciphertext = encrypted[3:]
    else:
        return encrypted

    if not ciphertext or len(ciphertext) % 16 != 0:
        return None

    key = derive_chromium_key(password)
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        return None


def get_secret_service_password(applications: tuple[str, ...] = ("Claude", "chrome", "chromium")) -> Optional[bytes]:
    """Fetch the Linux SecretService / KDE Wallet safeStorage password for Chromium/Electron."""
    if secretstorage is None:
        return None
    try:
        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        if collection.is_locked():
            collection.unlock()
        for app in applications:
            items = list(collection.search_items({"application": app}))
            for item in items:
                secret = item.get_secret()
                if secret:
                    return secret
        # Fallback: search by label containing "Safe Storage"
        for item in collection.get_all_items():
            label = item.get_label() or ""
            if "Safe Storage" in label:
                for app in applications:
                    if app.lower() in label.lower():
                        secret = item.get_secret()
                        if secret:
                            return secret
    except Exception:
        return None
    return None


def decrypt_electron_base64_value(b64_value: str, applications: tuple[str, ...] = ("Claude",)) -> Optional[str]:
    """Decrypt a base64-encoded Electron safeStorage string (e.g. config.json oauth:tokenCacheV2)."""
    if not b64_value:
        return None
    try:
        raw = base64.b64decode(b64_value)
    except Exception:
        return None
    v11_pw = get_secret_service_password(applications) if raw.startswith(b"v11") else None
    plain = decrypt_chromium_blob(raw, v11_password=v11_pw)
    if plain is None:
        return None
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


from dataclasses import dataclass


@dataclass(slots=True)
class ChromiumCookie:
    host_key: str
    name: str
    path: str
    is_secure: bool
    value: str


def read_chromium_cookie_records(
    cookies_db_path: str,
    host_filter: str,
    applications: tuple[str, ...] = ("Claude", "chrome", "chromium"),
) -> list[ChromiumCookie]:
    """Read and decrypt Chromium/Electron cookie records preserving host_key, path, and secure flag."""
    if not os.path.exists(cookies_db_path):
        return []

    fd, tmp_path = tempfile.mkstemp(prefix="ai_usage_cookies_", suffix=".db")
    os.close(fd)
    try:
        shutil.copy2(cookies_db_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        try:
            meta_version = 0
            try:
                row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                if row:
                    meta_version = int(row[0])
            except Exception:
                pass

            rows = conn.execute(
                "SELECT host_key, name, path, is_secure, value, encrypted_value FROM cookies WHERE host_key LIKE ?",
                (f"%{host_filter}%",),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    v11_pw: Optional[bytes] = None
    v11_checked = False
    records: list[ChromiumCookie] = []

    for host_key, name, path, is_secure, plain_val, enc_val in rows:
        if plain_val:
            records.append(ChromiumCookie(str(host_key), str(name), str(path or "/"), bool(is_secure), str(plain_val)))
            continue
        if not enc_val:
            continue
        raw_enc = bytes(enc_val)
        if raw_enc.startswith(b"v11") and not v11_checked:
            v11_pw = get_secret_service_password(applications)
            v11_checked = True
        decrypted = decrypt_chromium_blob(raw_enc, v11_password=v11_pw)
        if decrypted is None:
            continue
        if meta_version >= 24 and len(decrypted) >= 32:
            expected_prefix = hashlib.sha256(str(host_key).encode("utf-8")).digest()
            if decrypted[:32] == expected_prefix:
                decrypted = decrypted[32:]
        try:
            records.append(
                ChromiumCookie(str(host_key), str(name), str(path or "/"), bool(is_secure), decrypted.decode("utf-8"))
            )
        except UnicodeDecodeError:
            continue

    return records


def read_chromium_cookies(
    cookies_db_path: str,
    host_filter: str,
    applications: tuple[str, ...] = ("Claude", "chrome", "chromium"),
) -> dict[str, str]:
    """Read and decrypt Chromium/Electron cookies as a name -> value mapping."""
    return {
        c.name: c.value
        for c in read_chromium_cookie_records(cookies_db_path, host_filter, applications=applications)
    }


BROWSER_COOKIE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("~/.config/yandex-browser/*/Cookies", ("yandex", "Yandex", "yandex-browser", "chrome", "chromium")),
    ("~/.config/google-chrome/*/Cookies", ("chrome", "chromium")),
    ("~/.config/chromium/*/Cookies", ("chromium", "chrome")),
    ("~/.config/BraveSoftware/Brave-Browser/*/Cookies", ("brave", "Brave", "chrome", "chromium")),
    ("~/.config/vivaldi/*/Cookies", ("vivaldi", "Vivaldi", "chrome", "chromium")),
)


def find_browser_cookie_records(
    host_filter: str,
    required_cookie_names: tuple[str, ...],
) -> tuple[list[ChromiumCookie], Optional[str]]:
    """Search installed Chromium-based browsers for cookies matching host_filter and required names."""
    import glob

    for pattern, apps in BROWSER_COOKIE_PATTERNS:
        for db_path in sorted(glob.glob(os.path.expanduser(pattern))):
            records = read_chromium_cookie_records(db_path, host_filter, applications=apps)
            names = {r.name for r in records}
            if any(req in names for req in required_cookie_names):
                return records, db_path
    return [], None
