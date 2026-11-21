from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
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
from ai_usage_tray.core.chromium_crypto import (
    decrypt_electron_base64_value,
    find_browser_cookie_records,
    read_chromium_cookies,
)
from ai_usage_tray.core.config import ConfigManager
from ai_usage_tray.core.models import (
    ProviderUsageSnapshot,
    QuotaBucket,
    QuotaGroup,
    clamp_fraction,
    to_decimal,
)
from ai_usage_tray.core.provider import AuthStatus, UsageProvider

try:
    import keyring
except Exception:  # pragma: no cover
    keyring = None

CLAUDE_AUTH_URL = "https://claude.ai/oauth/authorize"
CLAUDE_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLAUDE_DEFAULT_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_SCOPES = ["user:profile", "user:inference"]
ANTHROPIC_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def _parse_iso(val: Any) -> Optional[datetime]:
    if not val or not isinstance(val, str):
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_claude_usage_payload(payload: dict[str, Any], source_label: str) -> ProviderUsageSnapshot:
    """Parse Anthropic OAuth /usage or claude.ai/api/organizations/{org}/usage payload."""
    window_defs = (
        ("five_hour", "5-Hour Limit Remaining", "5h"),
        ("seven_day", "Weekly Limit Remaining", "weekly"),
        ("seven_day_opus", "Opus Weekly Limit Remaining", "weekly"),
        ("seven_day_sonnet", "Sonnet Weekly Limit Remaining", "weekly"),
        ("seven_day_oauth_apps", "OAuth Apps Weekly Limit", "weekly"),
        ("iguana_necktie", "Extra Usage Window", "extra"),
    )

    buckets: list[QuotaBucket] = []
    for key, label, win_tag in window_defs:
        sec = payload.get(key)
        if not isinstance(sec, dict):
            continue
        util = sec.get("utilization")
        if util is None:
            continue
        used_pct = to_decimal(util)
        remaining = clamp_fraction(Decimal("1") - (used_pct / Decimal("100")))
        buckets.append(
            QuotaBucket(
                bucket_id=f"claude-{key}",
                display_name=label,
                window=win_tag,
                remaining_fraction=remaining,
                reset_time=_parse_iso(sec.get("resets_at")),
            )
        )

    badges: list[str] = []
    extra = payload.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used_c = extra.get("used_credits")
        limit_c = extra.get("monthly_limit")
        if used_c is not None and limit_c is not None:
            badges.append(f"Extra: {used_c}/{limit_c}")

    if not buckets and not badges:
        badges.append("No active usage windows")

    groups = [QuotaGroup(group_id="claude-limits", display_name="Claude Usage Limits", buckets=buckets)] if buckets else []

    return ProviderUsageSnapshot(
        provider_id="claude",
        provider_name="Claude",
        source_label=source_label,
        groups=groups,
        badges=badges,
    )


def _extract_token_from_json_blob(data: Any) -> Optional[OAuthTokens]:
    if not isinstance(data, dict):
        return None
    oauth_obj = data.get("claudeAiOauth", data)
    if isinstance(oauth_obj, dict) and oauth_obj.get("accessToken"):
        expires_ms = float(oauth_obj.get("expiresAt", 0) or 0)
        return OAuthTokens(
            access_token=str(oauth_obj["accessToken"]),
            refresh_token=str(oauth_obj.get("refreshToken", "")),
            expires_at=expires_ms / 1000.0 if expires_ms > 1e11 else expires_ms,
        )
    # Token cache dict keyed by scope/org
    for val in data.values():
        if isinstance(val, dict):
            tok = val.get("accessToken") or val.get("access_token")
            if tok:
                return OAuthTokens(
                    access_token=str(tok),
                    refresh_token=str(val.get("refreshToken") or val.get("refresh_token") or ""),
                )
    return None


def load_claude_code_credentials() -> Optional[OAuthTokens]:
    """Load OAuth tokens from ~/.claude/.credentials.json or system keyring."""
    cred_path = os.path.expanduser("~/.claude/.credentials.json")
    if os.path.exists(cred_path):
        try:
            with open(cred_path, "r", encoding="utf-8") as f:
                tok = _extract_token_from_json_blob(json.load(f))
                if tok:
                    return tok
        except Exception:
            pass

    if keyring is not None:
        try:
            raw = keyring.get_password("Claude Code-credentials", os.getenv("USER", ""))
            if not raw:
                raw = keyring.get_password("Claude Code-credentials", "claude")
            if raw:
                tok = _extract_token_from_json_blob(json.loads(raw))
                if tok:
                    return tok
        except Exception:
            pass
    return None


def load_claude_desktop_oauth_token() -> Optional[OAuthTokens]:
    """Decrypt oauth:tokenCacheV2 or oauth:tokenCache from ~/.config/Claude/config.json."""
    cfg_path = os.path.expanduser("~/.config/Claude/config.json")
    if not os.path.exists(cfg_path):
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    for key in ("oauth:tokenCacheV2", "oauth:tokenCache"):
        enc = data.get(key)
        if not isinstance(enc, str) or not enc:
            continue
        plain = decrypt_electron_base64_value(enc, applications=("Claude",))
        if not plain:
            continue
        try:
            parsed = json.loads(plain)
            tok = _extract_token_from_json_blob(parsed)
            if tok:
                return tok
        except Exception:
            continue
    return None


def load_claude_desktop_cookies() -> tuple[dict[str, str], Optional[str]]:
    """Extract session cookies and organization UUID from Claude Desktop or browser."""
    base = os.path.expanduser("~/.config/Claude")
    cookies = read_chromium_cookies(os.path.join(base, "Cookies"), "claude.ai", applications=("Claude",))
    if not cookies.get("sessionKey"):
        records, _ = find_browser_cookie_records("claude.ai", ("sessionKey",))
        if records:
            cookies = {r.name: r.value for r in records}

    org_id = cookies.get("lastActiveOrg")

    if not org_id:
        hist_path = os.path.join(base, "plan-usage-history.json")
        if os.path.exists(hist_path):
            try:
                with open(hist_path, "r", encoding="utf-8") as f:
                    hist = json.load(f)
                samples = hist.get("samples", [])
                if samples and isinstance(samples[-1], dict) and samples[-1].get("org"):
                    org_id = str(samples[-1]["org"])
            except Exception:
                pass

    return cookies, org_id


class ClaudeProvider(UsageProvider):
    provider_id = "claude"
    display_name = "Claude"
    supports_oauth = True
    supports_token_tracking = False
    default_poll_interval_s = 300

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager

    def get_auth_status(self) -> AuthStatus:
        mode = self.config_manager.config.claude.mode
        if mode != "desktop_only":
            if load_keyring_tokens(self.provider_id) is not None:
                return AuthStatus(True, "OAuth (keyring)")
            if load_claude_code_credentials() is not None:
                return AuthStatus(True, "Claude Code CLI credentials")
        if mode != "oauth_only":
            if load_claude_desktop_oauth_token() is not None:
                return AuthStatus(True, "Claude Desktop OAuth cache")
            cookies, org_id = load_claude_desktop_cookies()
            if cookies.get("sessionKey") and org_id:
                return AuthStatus(True, "Claude Desktop session cookie")
        return AuthStatus(False, "No Claude OAuth token or Desktop session found", needs_interactive_login=True)

    def start_oauth_login(self) -> tuple[bool, str]:
        cfg = self.config_manager.config.claude
        client_id = cfg.oauth_client_id or CLAUDE_DEFAULT_CLIENT_ID
        tokens, msg = run_pkce_loopback_flow(
            auth_url=CLAUDE_AUTH_URL,
            token_url=CLAUDE_TOKEN_URL,
            client_id=client_id,
            scopes=CLAUDE_SCOPES,
            extra_auth_params={"code": "true"},
        )
        if tokens is None:
            return False, msg
        save_keyring_tokens(self.provider_id, tokens)
        return True, "Connected to Claude OAuth."

    def disconnect_oauth(self) -> None:
        delete_keyring_tokens(self.provider_id)

    def fetch_usage(self) -> ProviderUsageSnapshot:
        mode = self.config_manager.config.claude.mode

        if mode in ("auto", "oauth_only"):
            oauth_snap = self._fetch_via_oauth()
            if oauth_snap is not None and not oauth_snap.auth_required:
                return oauth_snap
            if mode == "oauth_only":
                return oauth_snap or ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="OAuth",
                    auth_required=True,
                    error="No Claude OAuth token found.",
                )

        if mode in ("auto", "desktop_only"):
            desktop_snap = self._fetch_via_desktop_cookies()
            if desktop_snap is not None:
                return desktop_snap

        return ProviderUsageSnapshot(
            provider_id=self.provider_id,
            provider_name=self.display_name,
            source_label="Unauthenticated",
            auth_required=True,
            error="No Claude OAuth token or Claude Desktop sessionKey found.",
        )

    def _resolve_oauth_tokens(self) -> tuple[Optional[OAuthTokens], bool]:
        tok = load_keyring_tokens(self.provider_id)
        if tok is not None:
            return tok, True
        tok = load_claude_code_credentials()
        if tok is not None:
            return tok, False
        tok = load_claude_desktop_oauth_token()
        if tok is not None:
            return tok, False
        return None, False

    def _fetch_via_oauth(self) -> Optional[ProviderUsageSnapshot]:
        tokens, from_keyring = self._resolve_oauth_tokens()
        if tokens is None:
            return None

        cfg = self.config_manager.config.claude
        client_id = cfg.oauth_client_id or CLAUDE_DEFAULT_CLIENT_ID
        if tokens.is_expired() and tokens.refresh_token:
            refreshed = refresh_oauth_token(CLAUDE_TOKEN_URL, client_id, tokens.refresh_token)
            if refreshed is not None:
                tokens = refreshed
                if from_keyring:
                    save_keyring_tokens(self.provider_id, tokens)

        headers = {
            "Authorization": f"Bearer {tokens.access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": "ai-usage-tray/0.1.0",
        }
        try:
            resp = requests.get(ANTHROPIC_OAUTH_USAGE_URL, headers=headers, timeout=12)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "300") or "300")
                until = datetime.now(timezone.utc) + timedelta(seconds=max(60, retry_after))
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="OAuth",
                    rate_limited_until=until,
                    error=f"Rate limited (HTTP 429). Backing off for {retry_after}s.",
                )
            if resp.status_code == 401:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="OAuth",
                    auth_required=True,
                    error="Claude OAuth token expired or unauthorized.",
                )
            if resp.status_code >= 400:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="OAuth",
                    error=f"Claude OAuth API error ({resp.status_code}): {resp.text[:200]}",
                )
            return parse_claude_usage_payload(resp.json(), source_label="OAuth")
        except Exception as exc:
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="OAuth",
                error=f"Claude OAuth request failed: {exc}",
            )

    def _fetch_via_desktop_cookies(self) -> Optional[ProviderUsageSnapshot]:
        cookies, org_id = load_claude_desktop_cookies()
        session_key = cookies.get("sessionKey")
        if not session_key or not org_id:
            return None

        url = f"https://claude.ai/api/organizations/{org_id}/usage"
        cookie_pairs = [f"sessionKey={session_key}"]
        if cookies.get("cf_clearance"):
            cookie_pairs.append(f"cf_clearance={cookies['cf_clearance']}")
        if cookies.get("lastActiveOrg"):
            cookie_pairs.append(f"lastActiveOrg={cookies['lastActiveOrg']}")

        headers = {
            "Cookie": "; ".join(cookie_pairs),
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Claude/1.0.0 Chrome/130.0.0.0 Electron/33.0.0 Safari/537.36",
            "Referer": "https://claude.ai/settings/usage",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=12)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "300") or "300")
                until = datetime.now(timezone.utc) + timedelta(seconds=max(60, retry_after))
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Desktop Cookies",
                    rate_limited_until=until,
                    error=f"Rate limited (HTTP 429). Backing off for {retry_after}s.",
                )
            if resp.status_code in (401, 403):
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Desktop Cookies",
                    error=f"Claude Desktop cookie rejected ({resp.status_code}). Connect OAuth or refresh Claude Desktop.",
                )
            if resp.status_code >= 400:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Desktop Cookies",
                    error=f"Claude usage error ({resp.status_code}): {resp.text[:200]}",
                )
            return parse_claude_usage_payload(resp.json(), source_label="Desktop Cookies")
        except Exception as exc:
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="Desktop Cookies",
                error=f"Claude Desktop cookie request failed: {exc}",
            )
