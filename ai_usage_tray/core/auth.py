from __future__ import annotations

import base64
import hashlib
import html
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any, Optional

import requests

try:
    import keyring
except Exception:  # pragma: no cover
    keyring = None

KEYRING_SERVICE = "ai-usage-tray"


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    token_type: str = "Bearer"
    account_id: str = ""

    def is_expired(self, skew_seconds: int = 60) -> bool:
        if self.expires_at <= 0:
            return False
        return time.time() + skew_seconds >= self.expires_at

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "token_type": self.token_type,
                "account_id": self.account_id,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> Optional["OAuthTokens"]:
        try:
            data = json.loads(raw)
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get("access_token"):
            return None
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token", "")),
            expires_at=float(data.get("expires_at", 0.0) or 0.0),
            token_type=str(data.get("token_type", "Bearer")),
            account_id=str(data.get("account_id", "")),
        )


def load_keyring_tokens(provider_id: str) -> Optional[OAuthTokens]:
    if keyring is None:
        return None
    try:
        raw = keyring.get_password(KEYRING_SERVICE, f"{provider_id}:oauth")
    except Exception:
        return None
    if not raw:
        return None
    return OAuthTokens.from_json(raw)


def save_keyring_tokens(provider_id: str, tokens: OAuthTokens) -> bool:
    if keyring is None:
        return False
    try:
        keyring.set_password(KEYRING_SERVICE, f"{provider_id}:oauth", tokens.to_json())
        return True
    except Exception:
        return False


def delete_keyring_tokens(provider_id: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, f"{provider_id}:oauth")
    except Exception:
        pass


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def run_pkce_loopback_flow(
    auth_url: str,
    token_url: str,
    client_id: str,
    scopes: list[str],
    client_secret: str = "",
    extra_auth_params: Optional[dict[str, str]] = None,
    timeout_s: int = 180,
) -> tuple[Optional[OAuthTokens], str]:
    """Run an OAuth 2.0 PKCE authorization code flow via a local loopback callback."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    result: dict[str, Any] = {"code": None, "error": None}
    done = threading.Event()

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("state", [""])[0] != state:
                result["error"] = "OAuth state mismatch."
                self._send_html("Authentication failed: state mismatch. You may close this window.")
                done.set()
                return
            if "error" in qs:
                err = qs["error"][0]
                desc = qs.get("error_description", [""])[0]
                result["error"] = f"{err}: {desc}".strip(": ")
                self._send_html(f"Authentication failed: {html.escape(result['error'])}")
                done.set()
                return
            code = qs.get("code", [""])[0]
            if not code:
                result["error"] = "Missing authorization code."
                self._send_html("Authentication failed: missing authorization code.")
                done.set()
                return
            result["code"] = code
            self._send_html("Authentication successful! You can close this browser tab and return to AIUsageTray.")
            done.set()

        def _send_html(self, message: str) -> None:
            body = f"<html><body style='font-family:sans-serif;padding:2rem;'><h3>{message}</h3></body></html>".encode(
                "utf-8"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), CallbackHandler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if extra_auth_params:
            params.update(extra_auth_params)
        url = f"{auth_url}?{urllib.parse.urlencode(params)}"
        webbrowser.open(url)

        if not done.wait(timeout=timeout_s):
            return None, "OAuth login timed out waiting for browser callback."
        if result["error"]:
            return None, str(result["error"])
        code = str(result["code"])

        token_payload: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        }
        if client_secret:
            token_payload["client_secret"] = client_secret

        resp = requests.post(token_url, data=token_payload, timeout=20)
        if resp.status_code >= 400:
            return None, f"Token exchange failed ({resp.status_code}): {resp.text[:300]}"
        data = resp.json()
        expires_in = float(data.get("expires_in", 3600) or 3600)
        tokens = OAuthTokens(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token", "")),
            expires_at=time.time() + expires_in,
            token_type=str(data.get("token_type", "Bearer")),
        )
        return tokens, "Connected"
    except Exception as exc:
        return None, f"OAuth login error: {exc}"
    finally:
        server.shutdown()
        server.server_close()


def refresh_oauth_token(
    token_url: str,
    client_id: str,
    refresh_token: str,
    client_secret: str = "",
) -> Optional[OAuthTokens]:
    """Refresh an OAuth access token using a refresh_token."""
    if not refresh_token or not client_id:
        return None
    payload: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    try:
        resp = requests.post(token_url, data=payload, timeout=15)
        if resp.status_code >= 400:
            return None
        data = resp.json()
        access_token = data.get("access_token")
        if not access_token:
            return None
        expires_in = float(data.get("expires_in", 3600) or 3600)
        return OAuthTokens(
            access_token=str(access_token),
            refresh_token=str(data.get("refresh_token") or refresh_token),
            expires_at=time.time() + expires_in,
            token_type=str(data.get("token_type", "Bearer")),
        )
    except Exception:
        return None
