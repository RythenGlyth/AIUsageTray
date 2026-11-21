from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from ai_usage_tray.core.config import ConfigManager
from ai_usage_tray.core.google_web import (
    build_google_session,
    discover_rpcid_from_html,
    find_google_browser_cookies,
    parse_batchexecute_rpc,
)
from ai_usage_tray.core.models import (
    ProviderUsageSnapshot,
    QuotaBucket,
    QuotaGroup,
    clamp_fraction,
    to_decimal,
)
from ai_usage_tray.core.provider import AuthStatus, UsageProvider

GEMINI_BASE = "https://gemini.google.com"
GEMINI_BATCHEXECUTE_URL = f"{GEMINI_BASE}/_/BardChatUi/data/batchexecute"
GEMINI_GRAPHQL_URL = f"{GEMINI_BASE}/_/BardChatUi/graphql/schemas/GEMINI_WEB_GRAPHQL/executeQuery"
DEFAULT_QUOTA_RPCID = "qpEbW"
DEFAULT_USAGE_QUERY_SIG = "2/yL2v1aYbA346hkaeglNZmTMJfO37mMT1xFprjCS/Pj4="
DEFAULT_USAGE_VARIABLES = {
    "g1Request": {"entrypoints": ["GEMINI_USAGE_SETTINGS_UPGRADE_BANNER"]},
    "workspaceRequest": {
        "recommendationsForSurfaceRequests": [
            {
                "recommendationSurface": {"rpOnramp": "GEMINI_CHAT_USAGE_LIMITS_UPSELL", "version": "1"},
                "maxRecommendationCount": 1,
            }
        ]
    },
}


def _parse_iso(val: Any) -> Optional[datetime]:
    if not val or not isinstance(val, str):
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


def _classify_window(unit: Any, reset_dt: Optional[datetime], now: Optional[datetime] = None) -> tuple[str, str]:
    if reset_dt is not None:
        cur = now or datetime.now(timezone.utc)
        hours = (reset_dt - cur).total_seconds() / 3600.0
        if hours > 24.0:
            return "Weekly Limit Remaining", "weekly"
        return "5-Hour Limit Remaining", "5h"
    if unit == 2:
        return "5-Hour Limit Remaining", "5h"
    return "Weekly Limit Remaining", "weekly"


def parse_gemini_web_graphql(payload: Any) -> Optional[ProviderUsageSnapshot]:
    """Parse GEMINI_WEB_GRAPHQL UsagePageQuery response (`viewer.rpcs.getUsageInfo`)."""
    root = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(root, dict):
        return None
    rpcs = root.get("data", {}).get("viewer", {}).get("rpcs", {})
    usage_info = rpcs.get("getUsageInfo")
    if not isinstance(usage_info, dict):
        return None

    pools = usage_info.get("pools", [])
    if not isinstance(pools, list):
        return None

    buckets: list[QuotaBucket] = []
    for idx, pool in enumerate(pools):
        if not isinstance(pool, dict):
            continue
        pool_type = str(pool.get("poolType", f"pool-{idx}"))
        pct_used = pool.get("percentageUsed")
        if pct_used is None:
            continue

        # In GraphQL `percentageUsed` is a fraction in [0, 1] (e.g. 0.12393263 = 12.393263% used)
        used_dec = to_decimal(pct_used)
        if used_dec > Decimal("1"):
            used_dec = used_dec / Decimal("100")
        remaining = clamp_fraction(Decimal("1") - used_dec)

        gxu = pool.get("gxu") if isinstance(pool.get("gxu"), dict) else {}
        reset_dt = _parse_iso(gxu.get("windowRefreshesAt"))

        if "FIVE_HOURLY" in pool_type or "CURRENT" in pool_type:
            display_name, win_tag = "5-Hour Limit Remaining", "5h"
        elif "WEEKLY" in pool_type:
            display_name, win_tag = "Weekly Limit Remaining", "weekly"
        else:
            display_name, win_tag = pool_type.replace("_", " ").title(), pool_type.lower()

        buckets.append(
            QuotaBucket(
                bucket_id=f"gemini-web-{win_tag}",
                display_name=display_name,
                window=win_tag,
                remaining_fraction=remaining,
                reset_time=reset_dt,
            )
        )

    if not buckets:
        return None

    tier = usage_info.get("subscriptionTier")
    plan_name = str(tier).replace("TIER", "Tier ") if tier else None
    return ProviderUsageSnapshot(
        provider_id="gemini_web",
        provider_name="Gemini Web",
        source_label="Browser GraphQL",
        plan_name=plan_name,
        groups=[QuotaGroup(group_id="gemini-web-limits", display_name="Gemini Web Usage", buckets=buckets)],
    )


def parse_gemini_web_qpebw(data: Any, now: Optional[datetime] = None) -> ProviderUsageSnapshot:
    """Parse BardFrontendService.CheckGeminiQuota (`qpEbW`) fallback response."""
    rows = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else []
    buckets: list[QuotaBucket] = []

    for idx, row in enumerate(rows):
        if not isinstance(row, list) or len(row) < 3:
            continue
        meta = row[0] if isinstance(row[0], list) else []
        unit = row[1]
        percent_used = row[2]
        reset_arr = row[3] if len(row) > 3 else None
        limit_val = row[4] if len(row) > 4 else None
        remaining_val = row[5] if len(row) > 5 else None

        reset_dt: Optional[datetime] = None
        if isinstance(reset_arr, list) and reset_arr and isinstance(reset_arr[0], (int, float)):
            reset_dt = datetime.fromtimestamp(float(reset_arr[0]), tz=timezone.utc)

        action = meta[1] if len(meta) > 1 else None
        if action == 2 or not meta or meta == [None, 2]:
            label, win_tag = _classify_window(unit, reset_dt, now=now)
            bucket_id = f"gemini-web-{win_tag}"
        else:
            label = f"Limit #{idx + 1}"
            win_tag = f"limit-{idx + 1}"
            bucket_id = f"gemini-web-{win_tag}"

        desc = ""
        if isinstance(limit_val, (int, float)) and isinstance(remaining_val, (int, float)) and limit_val > 0:
            hi = max(limit_val, remaining_val)
            lo = min(limit_val, remaining_val)
            remaining = clamp_fraction(to_decimal(lo) / to_decimal(hi))
            desc = f"{lo} / {hi} remaining"
        elif percent_used is not None:
            remaining = clamp_fraction((Decimal("100") - to_decimal(percent_used)) / Decimal("100"))
        else:
            continue

        buckets.append(
            QuotaBucket(
                bucket_id=bucket_id,
                display_name=label,
                window=win_tag,
                remaining_fraction=remaining,
                reset_time=reset_dt,
                description=desc,
            )
        )

    groups = (
        [QuotaGroup(group_id="gemini-web-limits", display_name="Gemini Web Usage", buckets=buckets)]
        if buckets
        else []
    )
    return ProviderUsageSnapshot(
        provider_id="gemini_web",
        provider_name="Gemini Web",
        source_label="Browser Session",
        groups=groups,
        error=None if buckets else "No quota rows returned by Gemini Web.",
    )


def discover_usage_query_signature(session, usage_html: str) -> Optional[str]:
    """Scan UsagePageSurface-*.js linked from /usage HTML for the current UsagePageQuery signature."""
    urls = set(re.findall(r'https://www\.gstatic\.com/[^"\'\s\\<>]*UsagePageSurface[^"\'\s\\<>]*\.js[^"\'\s\\<>]*', usage_html))
    for url in sorted(urls):
        if "-messages-" in url:
            continue
        try:
            js = session.get(url, timeout=8).text
            m = re.search(r'id:\s*[`"\']([^`"\']+)[`"\'],\s*metadata:\s*\{\},\s*name:\s*[`"\']UsagePageQuery[`"\']', js)
            if m:
                return m.group(1)
        except Exception:
            continue
    return None


class GeminiWebProvider(UsageProvider):
    provider_id = "gemini_web"
    display_name = "Gemini Web"
    supports_oauth = False
    supports_token_tracking = False
    default_poll_interval_s = 300

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager
        self._cached_rpcid: str = DEFAULT_QUOTA_RPCID
        self._cached_query_sig: str = DEFAULT_USAGE_QUERY_SIG

    def get_auth_status(self) -> AuthStatus:
        records, db_path = find_google_browser_cookies()
        if records and db_path:
            return AuthStatus(True, f"Browser cookies ({os.path.basename(os.path.dirname(os.path.dirname(db_path)))})")
        return AuthStatus(False, "No Google browser session cookies found")

    def _fetch_via_graphql(self, session, sig: str) -> Optional[ProviderUsageSnapshot]:
        session.headers["Referer"] = f"{GEMINI_BASE}/usage"
        resp = session.get(
            GEMINI_GRAPHQL_URL,
            params={"query_signature": sig, "variables": json.dumps(DEFAULT_USAGE_VARIABLES)},
            timeout=12,
        )
        if resp.status_code != 200:
            return None
        try:
            return parse_gemini_web_graphql(resp.json())
        except Exception:
            return None

    def _call_quota_rpc(self, session, rpcid: str, at_token: str, bl: Optional[str]) -> Optional[Any]:
        f_req = json.dumps([[[rpcid, json.dumps([[[1, 2]]]), None, "generic"]]])
        params: dict[str, str] = {"rpcids": rpcid, "source-path": "/app", "rt": "c"}
        if bl:
            params["bl"] = bl
        resp = session.post(GEMINI_BATCHEXECUTE_URL, params=params, data={"f.req": f_req, "at": at_token}, timeout=12)
        if resp.status_code >= 400:
            return None
        return parse_batchexecute_rpc(resp.text, rpcid)

    def fetch_usage(self) -> ProviderUsageSnapshot:
        records, _ = find_google_browser_cookies()
        if not records:
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="Browser Session",
                error="No Google browser cookies found. Sign in to gemini.google.com in your browser.",
            )

        session = build_google_session(records, origin=GEMINI_BASE)
        try:
            snap = self._fetch_via_graphql(session, self._cached_query_sig)
            if snap is not None:
                return snap

            # Re-discover rotated GraphQL signature from /usage HTML
            usage_page = session.get(f"{GEMINI_BASE}/usage", timeout=12)
            if usage_page.status_code == 200:
                new_sig = discover_usage_query_signature(session, usage_page.text)
                if new_sig and new_sig != self._cached_query_sig:
                    self._cached_query_sig = new_sig
                    snap = self._fetch_via_graphql(session, new_sig)
                    if snap is not None:
                        return snap

            # Fallback to batchexecute qpEbW
            page = session.get(f"{GEMINI_BASE}/app", timeout=12)
            if page.status_code >= 400:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Browser Session",
                    error=f"Gemini Web page load failed ({page.status_code}).",
                )
            m_at = re.search(r'"SNlM0e":"([^"]+)"', page.text)
            if not m_at:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Browser Session",
                    error="Gemini Web session not signed in (missing SNlM0e).",
                )
            at_token = m_at.group(1)
            m_bl = re.search(r'"cfb2h":"([^"]+)"', page.text)
            bl = m_bl.group(1) if m_bl else None

            data = self._call_quota_rpc(session, self._cached_rpcid, at_token, bl)
            if data is None:
                discovered = discover_rpcid_from_html(session, page.text, ("CheckGeminiQuota",))
                if discovered and discovered != self._cached_rpcid:
                    self._cached_rpcid = discovered
                    data = self._call_quota_rpc(session, discovered, at_token, bl)

            if data is None:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Browser Session",
                    error="Could not fetch Gemini Web quota.",
                )
            return parse_gemini_web_qpebw(data)
        except Exception as exc:
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="Browser Session",
                error=f"Gemini Web request failed: {exc}",
            )
