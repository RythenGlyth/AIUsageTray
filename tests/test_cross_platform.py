import json
import os

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ai_usage_tray.core import chromium_crypto, config, google_web
from ai_usage_tray.providers import antigravity, claude, openai_codex


def test_config_dirs_windows(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\Alice\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Alice\AppData\Local")

    assert config.default_config_dir() == os.path.join(r"C:\Users\Alice\AppData\Roaming", "ai-usage-tray")
    assert config.default_data_dir() == os.path.join(r"C:\Users\Alice\AppData\Local", "ai-usage-tray")
    assert config.default_runtime_dir().endswith("ai-usage-tray")


def test_config_dirs_macos(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "darwin")
    if not hasattr(os, "getuid"):
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)

    assert config.default_config_dir().endswith(os.path.join("Library", "Application Support", "ai-usage-tray"))
    assert config.default_data_dir().endswith(os.path.join("Library", "Application Support", "ai-usage-tray"))
    assert "ai-usage-tray" in config.default_runtime_dir()


def test_decrypt_windows_v10_aes_gcm():
    key = b"0" * 32
    nonce = b"1" * 12
    ct = AESGCM(key).encrypt(nonce, b"win-session-cookie", None)
    blob = b"v10" + nonce + ct

    assert chromium_crypto.decrypt_windows_chromium_blob(blob, aes_key=key) == b"win-session-cookie"
    assert chromium_crypto.decrypt_windows_chromium_blob(b"v20" + nonce + ct, aes_key=key) is None


def test_decrypt_macos_v10_keychain(monkeypatch):
    monkeypatch.setattr(chromium_crypto.sys, "platform", "darwin")
    pw = b"macos-keychain-secret"
    key = chromium_crypto.derive_chromium_key(pw, iterations=1003)
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(b"mac-cookie") + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    blob = b"v10" + enc.update(padded) + enc.finalize()

    assert chromium_crypto.decrypt_chromium_blob(blob, v11_password=pw) == b"mac-cookie"


def test_antigravity_macos_parsers():
    ps_out = "  412 /Applications/Antigravity.app/Contents/MacOS/language_server --csrf_token tok123 --https_server_port 51234\n"
    servers = antigravity.parse_ps_command_lines_for_antigravity(ps_out)
    assert len(servers) == 1
    assert servers[0].pid == 412
    assert servers[0].csrf_token == "tok123"
    assert servers[0].explicit_port == 51234

    lsof_out = "language_ 412 user   12u  IPv4 0x123      0t0  TCP 127.0.0.1:51234 (LISTEN)\n"
    assert antigravity.parse_lsof_ports_for_pid(lsof_out) == [51234]


def test_antigravity_windows_parsers():
    cim_json = json.dumps(
        {
            "ProcessId": 9001,
            "CommandLine": r'"C:\Program Files\Antigravity\language_server.exe" --csrf_token=winTok --https_server_port=44321',
        }
    )
    servers = antigravity.parse_windows_cim_processes_for_antigravity(cim_json)
    assert len(servers) == 1
    assert servers[0].pid == 9001
    assert servers[0].csrf_token == "winTok"
    assert servers[0].explicit_port == 44321

    netstat_out = (
        "  TCP    127.0.0.1:44321        0.0.0.0:0              LISTENING       9001\n"
        "  TCP    127.0.0.1:9999         0.0.0.0:0              ESTABLISHED     9001\n"
    )
    assert antigravity.parse_windows_netstat_ports_for_pid(netstat_out, 9001) == [44321]


def test_platform_helpers(monkeypatch):
    monkeypatch.setattr(google_web.sys, "platform", "win32")
    assert "Windows NT" in google_web._platform_ua_token()

    monkeypatch.setattr(claude.sys, "platform", "darwin")
    assert claude._claude_desktop_dir().endswith(os.path.join("Library", "Application Support", "Claude"))

    monkeypatch.setattr(openai_codex.sys, "platform", "win32")
    assert any(p.endswith("codex.exe") for p in openai_codex._known_codex_paths())
