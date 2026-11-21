from __future__ import annotations

import json
import os
import re
import subprocess
import time
import webbrowser
from datetime import datetime
from typing import Any, Optional

import requests

from ai_usage_tray.core.auth import (
    OAuthTokens,
    delete_keyring_tokens,
    load_keyring_tokens,
    save_keyring_tokens,
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

COPILOT_USER_URL = "https://api.github.com/copilot_internal/user"
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"


def _parse_iso(val: Any) -> Optional[datetime]:
    if not val or not isinstance(val, str):
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_copilot_local_token() -> Optional[tuple[str, str]]:
    """Discover a GitHub Copilot OAuth token from local config files or gh CLI."""
    for filename in ("hosts.json", "apps.json"):
        path = os.path.expanduser(f"~/.config/github-copilot/{filename}")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for key, val in data.items():
                        if "github.com" in key and isinstance(val, dict) and val.get("oauth_token"):
                            return str(val["oauth_token"]), f"~/.config/github-copilot/{filename}"
            except Exception:
                pass

    gh_hosts = os.path.expanduser("~/.config/gh/hosts.yml")
    if os.path.exists(gh_hosts):
        try:
            with open(gh_hosts, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r"oauth_token:\s*(\S+)", content)
            if m:
                return m.group(1).strip(), "~/.config/gh/hosts.yml"
        except Exception:
            pass

    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=3)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip(), "gh CLI"
    except Exception:
        pass

    return None


def parse_copilot_user_response(payload: dict[str, Any], source_label: str) -> ProviderUsageSnapshot:
    """Parse `GET https://api.github.com/copilot_internal/user` response."""
    reset_dt = _parse_iso(payload.get("quota_reset_date_utc") or payload.get("quota_reset_date"))
    plan_raw = payload.get("copilot_plan")
    plan_name = str(plan_raw).replace("_", " ").title() if plan_raw else None

    snapshots = payload.get("quota_snapshots", {})
    buckets: list[QuotaBucket] = []
    badges: list[str] = []

    if isinstance(snapshots, dict):
        label_map = {
            "premium_interactions": "Premium Interactions Remaining",
            "chat": "Chat Quota Remaining",
            "completions": "Code Completions Remaining",
        }
        for key, info in snapshots.items():
            if not isinstance(info, dict):
                continue
            if info.get("unlimited"):
                badges.append(f"{key.replace('_', ' ').title()}: Unlimited")
                continue
            pct_rem = info.get("percent_remaining")
            entitlement = info.get("entitlement")
            remaining_count = info.get("remaining")
            if isinstance(entitlement, (int, float)) and isinstance(remaining_count, (int, float)) and entitlement > 0:
                frac = clamp_fraction(to_decimal(remaining_count) / to_decimal(entitlement))
                desc = f"{remaining_count} / {entitlement} remaining"
            elif pct_rem is not None:
                frac = clamp_fraction(to_decimal(pct_rem) / to_decimal("100"))
                desc = ""
            else:
                continue

            buckets.append(
                QuotaBucket(
                    bucket_id=f"copilot-{key}",
                    display_name=label_map.get(key, key.replace("_", " ").title()),
                    window="monthly",
                    remaining_fraction=frac,
                    reset_time=reset_dt,
                    description=desc,
                )
            )

    groups = (
        [QuotaGroup(group_id="copilot-quotas", display_name="GitHub Copilot Quotas", buckets=buckets)]
        if buckets
        else []
    )
    return ProviderUsageSnapshot(
        provider_id="github_copilot",
        provider_name="GitHub Copilot",
        source_label=source_label,
        plan_name=plan_name,
        badges=badges,
        groups=groups,
        error=None if (buckets or badges) else "No quota_snapshots returned by GitHub Copilot.",
    )


class GitHubCopilotProvider(UsageProvider):
    provider_id = "github_copilot"
    display_name = "GitHub Copilot"
    supports_oauth = True
    supports_token_tracking = False
    default_poll_interval_s = 300

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager

    def get_auth_status(self) -> AuthStatus:
        if load_keyring_tokens(self.provider_id) is not None:
            return AuthStatus(True, "GitHub OAuth (keyring)")
        local = load_copilot_local_token()
        if local is not None:
            return AuthStatus(True, f"Local token ({local[1]})")
        return AuthStatus(False, "No GitHub Copilot token found", needs_interactive_login=True)

    def start_oauth_login(self) -> tuple[bool, str]:
        cfg = self.config_manager.config.github_copilot
        client_id = cfg.oauth_client_id or GITHUB_COPILOT_CLIENT_ID
        try:
            dc_resp = requests.post(
                GITHUB_DEVICE_CODE_URL,
                headers={"Accept": "application/json"},
                data={"client_id": client_id, "scope": "read:user"},
                timeout=12,
            )
            if dc_resp.status_code >= 400:
                return False, f"GitHub device flow failed ({dc_resp.status_code})."
            dc = dc_resp.json()
            verification_uri = dc.get("verification_uri", "https://github.com/login/device")
            user_code = dc.get("user_code", "")
            device_code = dc.get("device_code", "")
            interval = max(5, int(dc.get("interval", 5) or 5))
            expires_in = int(dc.get("expires_in", 300) or 300)
            webbrowser.open(verification_uri)

            deadline = time.time() + min(expires_in, 180)
            while time.time() < deadline:
                time.sleep(interval)
                tok_resp = requests.post(
                    GITHUB_ACCESS_TOKEN_URL,
                    headers={"Accept": "application/json"},
                    data={
                        "client_id": client_id,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                    timeout=12,
                )
                tdata = tok_resp.json()
                if tdata.get("access_token"):
                    save_keyring_tokens(
                        self.provider_id,
                        OAuthTokens(access_token=str(tdata["access_token"])),
                    )
                    return True, f"Connected to GitHub Copilot (code {user_code})."
                if tdata.get("error") not in ("authorization_pending", "slow_down"):
                    return False, str(tdata.get("error_description") or tdata.get("error") or "OAuth failed.")
            return False, f"Timed out waiting for GitHub device authorization (code: {user_code})."
        except Exception as exc:
            return False, f"GitHub OAuth error: {exc}"

    def disconnect_oauth(self) -> None:
        delete_keyring_tokens(self.provider_id)

    def fetch_usage(self) -> ProviderUsageSnapshot:
        kr = load_keyring_tokens(self.provider_id)
        if kr is not None:
            token, source = kr.access_token, "OAuth (keyring)"
        else:
            local = load_copilot_local_token()
            if local is None:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="OAuth",
                    auth_required=True,
                    error="No GitHub Copilot token found. Connect OAuth or run `gh auth login`.",
                )
            token, source = local

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Editor-Version": "vscode/1.107.0",
            "Editor-Plugin-Version": "copilot-chat/0.35.0",
            "Copilot-Integration-Id": "vscode-chat",
        }
        try:
            resp = requests.get(COPILOT_USER_URL, headers=headers, timeout=12)
            if resp.status_code in (401, 403):
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label=source,
                    auth_required=True,
                    error=f"GitHub Copilot token rejected ({resp.status_code}).",
                )
            if resp.status_code >= 400:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label=source,
                    error=f"GitHub Copilot API error ({resp.status_code}): {resp.text[:200]}",
                )
            return parse_copilot_user_response(resp.json(), source_label=source)
        except Exception as exc:
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label=source,
                error=f"GitHub Copilot request failed: {exc}",
            )
