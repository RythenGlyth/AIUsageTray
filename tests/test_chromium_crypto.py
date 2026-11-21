import hashlib
import sqlite3

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ai_usage_tray.core.chromium_crypto import (
    decrypt_chromium_blob,
    derive_chromium_key,
    read_chromium_cookies,
)


def _encrypt_v10(plaintext: bytes) -> bytes:
    key = derive_chromium_key(b"peanuts")
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
    enc = cipher.encryptor()
    return b"v10" + enc.update(padded) + enc.finalize()


def test_decrypt_v10_blob():
    enc = _encrypt_v10(b"secret-session-value")
    assert decrypt_chromium_blob(enc) == b"secret-session-value"


def test_read_chromium_cookies_v24_prefix(tmp_path):
    db_path = tmp_path / "Cookies"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta (key, value) VALUES ('version', '24')")
    conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, path TEXT, is_secure INTEGER, value TEXT, encrypted_value BLOB)")

    host_key = ".claude.ai"
    prefix = hashlib.sha256(host_key.encode("utf-8")).digest()
    enc_val = _encrypt_v10(prefix + b"sk-ant-sid01-test")
    conn.execute("INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?)", (host_key, "sessionKey", "/", 1, "", enc_val))
    conn.commit()
    conn.close()

    cookies = read_chromium_cookies(str(db_path), "claude.ai")
    assert cookies["sessionKey"] == "sk-ant-sid01-test"
