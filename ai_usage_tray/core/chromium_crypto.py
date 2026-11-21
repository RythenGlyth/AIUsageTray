from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    import secretstorage
except Exception:  # pragma: no cover
    secretstorage = None


def _default_pbkdf2_iterations() -> int:
    return 1003 if sys.platform == "darwin" else 1


def derive_chromium_key(password: bytes, iterations: Optional[int] = None) -> bytes:
    """Derive 16-byte AES-128-CBC key used by POSIX Chromium/Electron safeStorage."""
    iters = _default_pbkdf2_iterations() if iterations is None else iterations
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", iters, dklen=16)


def decrypt_chromium_blob(
    encrypted: bytes,
    v11_password: Optional[bytes] = None,
    iterations: Optional[int] = None,
) -> Optional[bytes]:
    """Decrypt a POSIX Chromium/Electron v10 or v11 AES-128-CBC encrypted value."""
    if not encrypted:
        return None
    prefix = encrypted[:3]
    if prefix == b"v10":
        # On macOS, Chromium encrypts v10 with the Keychain Safe Storage password when available.
        password = v11_password if (sys.platform == "darwin" and v11_password) else b"peanuts"
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

    key = derive_chromium_key(password, iterations=iterations)
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


_MACOS_KEYCHAIN_SERVICES: dict[str, str] = {
    "claude": "Claude Safe Storage",
    "chrome": "Chrome Safe Storage",
    "chromium": "Chromium Safe Storage",
    "brave": "Brave Safe Storage",
    "edge": "Microsoft Edge Safe Storage",
    "vivaldi": "Vivaldi Safe Storage",
    "yandex": "Yandex Safe Storage",
    "yandex-browser": "Yandex Safe Storage",
}


def get_macos_keychain_password(applications: tuple[str, ...] = ("Claude", "chrome", "chromium")) -> Optional[bytes]:
    """Fetch the macOS Keychain Safe Storage password for Chromium/Electron apps."""
    if sys.platform != "darwin":
        return None
    seen_services: set[str] = set()
    for app in applications:
        service = _MACOS_KEYCHAIN_SERVICES.get(app.lower(), f"{app} Safe Storage")
        if service in seen_services:
            continue
        seen_services.add(service)
        try:
            proc = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-w", "-s", service],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                pw = proc.stdout.rstrip("\r\n")
                if pw:
                    return pw.encode("utf-8")
        except Exception:
            continue
    return None


def _dpapi_decrypt(data: bytes) -> Optional[bytes]:
    """Decrypt a Windows DPAPI blob using crypt32.CryptUnprotectData."""
    if sys.platform != "win32" or not data:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        in_buf = ctypes.create_string_buffer(data, len(data))
        in_blob = DATA_BLOB(len(data), in_buf)
        out_blob = DATA_BLOB()

        if not crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        ):
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return None


def _find_local_state_path(target_path: str) -> Optional[str]:
    cur = os.path.dirname(os.path.abspath(target_path))
    for _ in range(4):
        candidate = os.path.join(cur, "Local State")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _load_windows_local_state_key(target_path: str) -> Optional[bytes]:
    local_state_path = _find_local_state_path(target_path)
    if not local_state_path:
        return None
    try:
        with open(local_state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        enc_b64 = data.get("os_crypt", {}).get("encrypted_key", "")
        if not enc_b64:
            return None
        raw = base64.b64decode(enc_b64)
        if raw.startswith(b"DPAPI"):
            raw = raw[5:]
        return _dpapi_decrypt(raw)
    except Exception:
        return None


def decrypt_windows_chromium_blob(encrypted: bytes, aes_key: Optional[bytes] = None) -> Optional[bytes]:
    """Decrypt a Windows Chromium/Electron v10/v11 AES-256-GCM or legacy DPAPI blob."""
    if not encrypted:
        return None
    prefix = encrypted[:3]
    if prefix in (b"v10", b"v11"):
        if not aes_key or len(encrypted) <= 15:
            return None
        nonce = encrypted[3:15]
        ciphertext = encrypted[15:]
        try:
            return AESGCM(aes_key).decrypt(nonce, ciphertext, None)
        except Exception:
            return None
    if prefix == b"v20":
        # Chrome App-Bound Encryption requires elevated IElevator COM; cannot decrypt from user tray.
        return None
    return _dpapi_decrypt(encrypted) or encrypted


def decrypt_electron_base64_value(
    b64_value: str,
    applications: tuple[str, ...] = ("Claude",),
    local_state_hint: Optional[str] = None,
) -> Optional[str]:
    """Decrypt a base64-encoded Electron safeStorage string (e.g. config.json oauth:tokenCacheV2)."""
    if not b64_value:
        return None
    try:
        raw = base64.b64decode(b64_value)
    except Exception:
        return None

    if sys.platform == "win32":
        aes_key = _load_windows_local_state_key(local_state_hint) if local_state_hint else None
        plain = decrypt_windows_chromium_blob(raw, aes_key=aes_key)
    elif sys.platform == "darwin":
        pw = get_macos_keychain_password(applications) if raw[:3] in (b"v10", b"v11") else None
        plain = decrypt_chromium_blob(raw, v11_password=pw)
    else:
        v11_pw = get_secret_service_password(applications) if raw.startswith(b"v11") else None
        plain = decrypt_chromium_blob(raw, v11_password=v11_pw)

    if plain is None:
        return None
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


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

    win_key: Optional[bytes] = None
    win_key_checked = False
    posix_pw: Optional[bytes] = None
    posix_pw_checked = False
    records: list[ChromiumCookie] = []

    for host_key, name, path, is_secure, plain_val, enc_val in rows:
        if plain_val:
            records.append(ChromiumCookie(str(host_key), str(name), str(path or "/"), bool(is_secure), str(plain_val)))
            continue
        if not enc_val:
            continue
        raw_enc = bytes(enc_val)

        if sys.platform == "win32":
            if raw_enc[:3] in (b"v10", b"v11") and not win_key_checked:
                win_key = _load_windows_local_state_key(cookies_db_path)
                win_key_checked = True
            decrypted = decrypt_windows_chromium_blob(raw_enc, aes_key=win_key)
        elif sys.platform == "darwin":
            if raw_enc[:3] in (b"v10", b"v11") and not posix_pw_checked:
                posix_pw = get_macos_keychain_password(applications)
                posix_pw_checked = True
            decrypted = decrypt_chromium_blob(raw_enc, v11_password=posix_pw)
        else:
            if raw_enc.startswith(b"v11") and not posix_pw_checked:
                posix_pw = get_secret_service_password(applications)
                posix_pw_checked = True
            decrypted = decrypt_chromium_blob(raw_enc, v11_password=posix_pw)

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


def _browser_cookie_patterns() -> tuple[tuple[str, tuple[str, ...]], ...]:
    if sys.platform == "win32":
        local = os.getenv("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return (
            (os.path.join(local, "Google", "Chrome", "User Data", "*", "Network", "Cookies"), ("chrome", "chromium")),
            (os.path.join(local, "Google", "Chrome", "User Data", "*", "Cookies"), ("chrome", "chromium")),
            (os.path.join(local, "Microsoft", "Edge", "User Data", "*", "Network", "Cookies"), ("edge", "chrome")),
            (os.path.join(local, "Microsoft", "Edge", "User Data", "*", "Cookies"), ("edge", "chrome")),
            (os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data", "*", "Network", "Cookies"), ("brave", "chrome")),
            (os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data", "*", "Cookies"), ("brave", "chrome")),
            (os.path.join(local, "Chromium", "User Data", "*", "Network", "Cookies"), ("chromium", "chrome")),
            (os.path.join(local, "Chromium", "User Data", "*", "Cookies"), ("chromium", "chrome")),
            (os.path.join(local, "Vivaldi", "User Data", "*", "Network", "Cookies"), ("vivaldi", "chrome")),
            (os.path.join(local, "Vivaldi", "User Data", "*", "Cookies"), ("vivaldi", "chrome")),
            (os.path.join(local, "Yandex", "YandexBrowser", "User Data", "*", "Network", "Cookies"), ("yandex", "chrome")),
            (os.path.join(local, "Yandex", "YandexBrowser", "User Data", "*", "Cookies"), ("yandex", "chrome")),
        )
    if sys.platform == "darwin":
        return (
            ("~/Library/Application Support/Google/Chrome/*/Cookies", ("chrome", "chromium")),
            ("~/Library/Application Support/Microsoft Edge/*/Cookies", ("edge", "chrome")),
            ("~/Library/Application Support/BraveSoftware/Brave-Browser/*/Cookies", ("brave", "Brave", "chrome")),
            ("~/Library/Application Support/Chromium/*/Cookies", ("chromium", "chrome")),
            ("~/Library/Application Support/Vivaldi/*/Cookies", ("vivaldi", "Vivaldi", "chrome")),
            ("~/Library/Application Support/Yandex/YandexBrowser/*/Cookies", ("yandex", "Yandex", "chrome")),
        )
    return (
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
    for pattern, apps in _browser_cookie_patterns():
        for db_path in sorted(glob.glob(os.path.expanduser(pattern))):
            records = read_chromium_cookie_records(db_path, host_filter, applications=apps)
            names = {r.name for r in records}
            if any(req in names for req in required_cookie_names):
                return records, db_path
    return [], None
