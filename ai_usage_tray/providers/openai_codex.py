from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import requests

from ai_usage_tray.core.auth import (
    OAuthTokens,
    delete_keyring_tokens,
    load_keyring_tokens,
    refresh_oauth_token,
    run_pkce_loopback_flow,
    save_keyring_tokens,
)
from ai_usage_tray.core.chromium_crypto import find_browser_cookie_records
from ai_usage_tray.core.config import ConfigManager
from ai_usage_tray.core.models import (
    ProviderUsageSnapshot,
    QuotaBucket,
    QuotaGroup,
    clamp_fraction,
    to_decimal,
)
from ai_usage_tray.core.provider import AuthStatus, UsageProvider

OPENAI_AUTH_URL = "https://auth.openai.com/oauth/authorize"
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_SCOPES = ["openid", "profile", "email", "offline_access"]
CHATGPT_WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

KNOWN_CODEX_PATHS = (
    "/usr/lib/chatgpt/resources/codex",
    "/opt/ChatGPT/resources/codex",
    "/opt/chatgpt/resources/codex",
    os.path.expanduser("~/.local/bin/codex"),
)


def _window_label(duration_mins: Optional[int], default_label: str) -> tuple[str, str]:
    if duration_mins is None:
        return default_label, ""
    if duration_mins <= 360:
        hours = duration_mins // 60
        return f"{hours}-Hour Limit Remaining" if hours else f"{duration_mins}-Min Limit Remaining", f"{hours}h"
    if duration_mins >= 10000:
        return "Weekly Limit Remaining", "weekly"
    days = duration_mins // 1440
    return f"{days}-Day Limit Remaining", f"{days}d"


def _epoch_to_datetime(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


def find_codex_binary(configured_path: str = "", allow_slow_search: bool = True) -> Optional[str]:
    """Locate the codex CLI binary via configured path, PATH, known app paths, or bounded search."""
    if configured_path and os.path.isfile(configured_path) and os.access(configured_path, os.X_OK):
        return configured_path

    on_path = shutil.which("codex")
    if on_path:
        return on_path

    for candidate in KNOWN_CODEX_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    if not allow_slow_search:
        return None

    search_roots = [p for p in ("/opt", "/usr/lib", "/usr/share", os.path.expanduser("~/.local")) if os.path.exists(p)]
    if not search_roots:
        return None
    try:
        proc = subprocess.run(
            ["find", *search_roots, "-maxdepth", "4", "-name", "codex", "-type", "f", "-executable"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in proc.stdout.splitlines():
            path = line.strip()
            if path and os.path.isfile(path):
                return path
    except Exception:
        pass
    return None


def parse_codex_app_server_result(result: dict[str, Any]) -> ProviderUsageSnapshot:
    """Parse `account/rateLimits/read` JSON-RPC result from `codex app-server`."""
    limits_map = result.get("rateLimitsByLimitId")
    entries: list[dict[str, Any]] = []
    if isinstance(limits_map, dict) and limits_map:
        entries = [v for v in limits_map.values() if isinstance(v, dict)]
    elif isinstance(result.get("rateLimits"), dict):
        entries = [result["rateLimits"]]

    groups: list[QuotaGroup] = []
    plan_name: Optional[str] = None
    badges: list[str] = []

    for entry in entries:
        limit_id = str(entry.get("limitId") or "codex")
        limit_name = entry.get("limitName") or limit_id.replace("_", " ").title()
        if not plan_name and entry.get("planType"):
            plan_name = str(entry["planType"]).title()

        buckets: list[QuotaBucket] = []
        for key, fallback_label in (("primary", "Primary Limit Remaining"), ("secondary", "Weekly Limit Remaining")):
            win = entry.get(key)
            if not isinstance(win, dict):
                continue
            used_pct = to_decimal(win.get("usedPercent", 0))
            remaining = clamp_fraction(Decimal("1") - (used_pct / Decimal("100")))
            display_name, win_tag = _window_label(win.get("windowDurationMins"), fallback_label)
            buckets.append(
                QuotaBucket(
                    bucket_id=f"{limit_id}-{key}",
                    display_name=display_name,
                    window=win_tag,
                    remaining_fraction=remaining,
                    reset_time=_epoch_to_datetime(win.get("resetsAt")),
                )
            )

        credits_info = entry.get("credits")
        if isinstance(credits_info, dict) and credits_info.get("hasCredits"):
            if credits_info.get("unlimited"):
                badges.append("Unlimited Credits")
            elif credits_info.get("balance") is not None:
                badges.append(f"Credits: {credits_info['balance']}")

        if buckets:
            groups.append(
                QuotaGroup(
                    group_id=f"codex-{limit_id}",
                    display_name=str(limit_name),
                    buckets=buckets,
                )
            )

    reset_credits = result.get("rateLimitResetCredits")
    if isinstance(reset_credits, dict):
        avail = reset_credits.get("availableCount")
        if isinstance(avail, int) and avail > 0:
            badges.append(f"{avail} Reset Credit{'s' if avail != 1 else ''}")

    return ProviderUsageSnapshot(
        provider_id="openai_codex",
        provider_name="Codex (OpenAI)",
        source_label="Codex Binary",
        groups=groups,
        plan_name=plan_name,
        badges=badges,
    )


def parse_wham_usage_response(payload: dict[str, Any]) -> ProviderUsageSnapshot:
    """Parse `GET https://chatgpt.com/backend-api/wham/usage` response."""
    groups: list[QuotaGroup] = []
    plan_name = str(payload["plan_type"]).title() if payload.get("plan_type") else None
    badges: list[str] = []

    def _extract_group(section_key: str, group_title: str) -> None:
        sec = payload.get(section_key)
        if not isinstance(sec, dict):
            return
        buckets: list[QuotaBucket] = []
        for win_key, fallback_label in (("primary_window", "5-Hour Limit Remaining"), ("secondary_window", "Weekly Limit Remaining")):
            win = sec.get(win_key)
            if not isinstance(win, dict):
                continue
            used_pct = to_decimal(win.get("used_percent", 0))
            remaining = clamp_fraction(Decimal("1") - (used_pct / Decimal("100")))
            limit_secs = win.get("limit_window_seconds")
            mins = int(limit_secs) // 60 if isinstance(limit_secs, (int, float)) else None
            display_name, win_tag = _window_label(mins, fallback_label)
            buckets.append(
                QuotaBucket(
                    bucket_id=f"{section_key}-{win_key}",
                    display_name=display_name,
                    window=win_tag,
                    remaining_fraction=remaining,
                    reset_time=_epoch_to_datetime(win.get("reset_at")),
                )
            )
        if buckets:
            groups.append(QuotaGroup(group_id=section_key, display_name=group_title, buckets=buckets))

    _extract_group("rate_limit", "Codex")
    _extract_group("code_review_rate_limit", "Code Review")

    credits_info = payload.get("credits")
    if isinstance(credits_info, dict) and credits_info.get("has_credits"):
        if credits_info.get("unlimited"):
            badges.append("Unlimited Credits")
        elif credits_info.get("balance") is not None:
            badges.append(f"Credits: {credits_info['balance']}")

    return ProviderUsageSnapshot(
        provider_id="openai_codex",
        provider_name="Codex (OpenAI)",
        source_label="OAuth",
        groups=groups,
        plan_name=plan_name,
        badges=badges,
    )


def load_codex_cli_auth() -> Optional[OAuthTokens]:
    """Load OAuth tokens from ~/.codex/auth.json if present."""
    path = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        tokens = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            return None
        return OAuthTokens(
            access_token=str(tokens["access_token"]),
            refresh_token=str(tokens.get("refresh_token", "")),
            account_id=str(tokens.get("account_id", "")),
        )
    except Exception:
        return None


class OpenAICodexProvider(UsageProvider):
    provider_id = "openai_codex"
    display_name = "Codex (OpenAI)"
    supports_oauth = True
    supports_token_tracking = False
    default_poll_interval_s = 60

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager

    def _resolve_codex_binary(self) -> Optional[str]:
        cfg = self.config_manager.config.openai_codex
        found = find_codex_binary(cfg.binary_path, allow_slow_search=not cfg.binary_path)
        if found and found != cfg.binary_path:
            cfg.binary_path = found
            self.config_manager.save()
        return found

    def get_auth_status(self) -> AuthStatus:
        mode = self.config_manager.config.openai_codex.mode
        codex_bin = find_codex_binary(self.config_manager.config.openai_codex.binary_path, allow_slow_search=False)
        if mode in ("auto", "codex_binary") and codex_bin:
            return AuthStatus(True, f"Codex binary ({codex_bin})")
        if load_keyring_tokens(self.provider_id) is not None:
            return AuthStatus(True, "OAuth (keyring)")
        if load_codex_cli_auth() is not None:
            return AuthStatus(True, "Codex CLI (~/.codex/auth.json)")
        records, db_path = find_browser_cookie_records(
            "chatgpt.com", ("__Secure-next-auth.session-token", "__Secure-next-auth.session-token.0")
        )
        if records and db_path:
            return AuthStatus(True, f"Browser session ({os.path.basename(os.path.dirname(os.path.dirname(db_path)))})")
        return AuthStatus(False, "No Codex binary, OAuth token, or browser session found", needs_interactive_login=True)

    def start_oauth_login(self) -> tuple[bool, str]:
        cfg = self.config_manager.config.openai_codex
        client_id = cfg.oauth_client_id or OPENAI_DEFAULT_CLIENT_ID
        tokens, msg = run_pkce_loopback_flow(
            auth_url=OPENAI_AUTH_URL,
            token_url=OPENAI_TOKEN_URL,
            client_id=client_id,
            scopes=OPENAI_SCOPES,
            extra_auth_params={"codex_cli_simplified_flow": "true"},
        )
        if tokens is None:
            return False, msg
        save_keyring_tokens(self.provider_id, tokens)
        return True, "Connected to OpenAI Codex OAuth."

    def disconnect_oauth(self) -> None:
        delete_keyring_tokens(self.provider_id)

    def fetch_usage(self) -> ProviderUsageSnapshot:
        mode = self.config_manager.config.openai_codex.mode

        if mode in ("auto", "codex_binary"):
            bin_snap = self._fetch_from_codex_binary()
            if bin_snap is not None and not bin_snap.error:
                return bin_snap
            if mode == "codex_binary":
                return bin_snap or ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Codex Binary",
                    error="Codex binary not found.",
                )

        if mode in ("auto", "oauth"):
            oauth_snap = self._fetch_from_oauth()
            if not oauth_snap.auth_required or mode == "oauth":
                return oauth_snap

        if mode in ("auto", "browser"):
            browser_snap = self._fetch_from_browser_cookies()
            if browser_snap is not None:
                return browser_snap
            if mode == "browser":
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Browser Session",
                    error="No ChatGPT browser session cookies found.",
                )

        return self._fetch_from_oauth()

    def _fetch_from_browser_cookies(self) -> Optional[ProviderUsageSnapshot]:
        records, _ = find_browser_cookie_records(
            "chatgpt.com", ("__Secure-next-auth.session-token", "__Secure-next-auth.session-token.0")
        )
        if not records:
            return None

        session = requests.Session()
        session.headers["User-Agent"] = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        for c in records:
            session.cookies.set(c.name, c.value, domain=c.host_key, path=c.path, secure=c.is_secure)

        try:
            sess_resp = session.get("https://chatgpt.com/api/auth/session", timeout=12)
            if sess_resp.status_code != 200:
                return None
            access_token = sess_resp.json().get("accessToken")
            if not access_token:
                return None
            resp = session.get(
                CHATGPT_WHAM_USAGE_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                timeout=12,
            )
            if resp.status_code >= 400:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Browser Session",
                    error=f"ChatGPT browser usage error ({resp.status_code}).",
                )
            snap = parse_wham_usage_response(resp.json())
            snap.source_label = "Browser Session"
            return snap
        except Exception:
            return None

    def _fetch_from_codex_binary(self) -> Optional[ProviderUsageSnapshot]:
        codex_bin = self._resolve_codex_binary()
        if not codex_bin:
            return None
        proc: Optional[subprocess.Popen[str]] = None
        try:
            proc = subprocess.Popen(
                [codex_bin, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            assert proc.stdin is not None and proc.stdout is not None

            def send(msg: dict[str, Any]) -> None:
                assert proc is not None and proc.stdin is not None
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()

            send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "ai-usage-tray", "version": "0.1.0"}}})
            proc.stdout.readline()
            send({"method": "initialized", "params": {}})
            send({"id": 2, "method": "account/rateLimits/read", "params": {}})

            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                data = json.loads(line)
                if data.get("id") == 2:
                    if "error" in data:
                        return ProviderUsageSnapshot(
                            provider_id=self.provider_id,
                            provider_name=self.display_name,
                            source_label="Codex Binary",
                            error=str(data["error"]),
                        )
                    return parse_codex_app_server_result(data.get("result", {}))

            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="Codex Binary",
                error="Codex app-server closed without returning rate limits.",
            )
        except Exception as exc:
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="Codex Binary",
                error=f"Codex binary query failed: {exc}",
            )
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    def _fetch_from_oauth(self) -> ProviderUsageSnapshot:
        from_keyring = True
        tokens = load_keyring_tokens(self.provider_id)
        if tokens is None:
            tokens = load_codex_cli_auth()
            from_keyring = False
        if tokens is None:
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="OAuth",
                auth_required=True,
                error="No Codex OAuth token found. Click Connect OAuth in Settings.",
            )

        cfg = self.config_manager.config.openai_codex
        client_id = cfg.oauth_client_id or OPENAI_DEFAULT_CLIENT_ID
        if tokens.is_expired() and tokens.refresh_token:
            refreshed = refresh_oauth_token(OPENAI_TOKEN_URL, client_id, tokens.refresh_token)
            if refreshed is not None:
                refreshed.account_id = tokens.account_id
                tokens = refreshed
                if from_keyring:
                    save_keyring_tokens(self.provider_id, tokens)

        headers = {
            "Authorization": f"Bearer {tokens.access_token}",
            "Accept": "application/json",
        }
        if tokens.account_id:
            headers["ChatGPT-Account-Id"] = tokens.account_id

        try:
            resp = requests.get(CHATGPT_WHAM_USAGE_URL, headers=headers, timeout=12)
            if resp.status_code == 401 and tokens.refresh_token:
                refreshed = refresh_oauth_token(OPENAI_TOKEN_URL, client_id, tokens.refresh_token)
                if refreshed is not None:
                    refreshed.account_id = tokens.account_id
                    tokens = refreshed
                    if from_keyring:
                        save_keyring_tokens(self.provider_id, tokens)
                    headers["Authorization"] = f"Bearer {tokens.access_token}"
                    resp = requests.get(CHATGPT_WHAM_USAGE_URL, headers=headers, timeout=12)

            if resp.status_code == 401:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="OAuth",
                    auth_required=True,
                    error="OpenAI OAuth token expired or unauthorized.",
                )
            if resp.status_code >= 400:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="OAuth",
                    error=f"OpenAI usage API error ({resp.status_code}): {resp.text[:200]}",
                )
            return parse_wham_usage_response(resp.json())
        except Exception as exc:
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="OAuth",
                error=f"OpenAI usage request failed: {exc}",
            )
