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

NOTEBOOK_BASE = "https://notebook.google.com"
BATCHEXECUTE_URL = f"{NOTEBOOK_BASE}/_/LabsTailwindUi/data/batchexecute"


def parse_notebooklm_eyldcb(
    eyldcb_data: Any,
    tier_raw: Optional[str] = None,
) -> ProviderUsageSnapshot:
    """Parse NotebookLM `EylDcb` RPC response into a ProviderUsageSnapshot."""
    buckets: list[QuotaBucket] = []

    raw_windows = eyldcb_data[1] if isinstance(eyldcb_data, list) and len(eyldcb_data) > 1 else []
    if isinstance(raw_windows, list):
        for idx, entry in enumerate(raw_windows):
            if not isinstance(entry, list) or len(entry) < 8:
                continue
            win_code = entry[4]
            reset_arr = entry[5]
            pct_val = entry[7]
            if pct_val is None:
                continue

            if win_code == 1:
                bucket_id = "notebooklm-5h"
                display_name = "5-Hour Limit Remaining"
                window = "5h"
            elif win_code == 2:
                bucket_id = "notebooklm-weekly"
                display_name = "Weekly Limit Remaining"
                window = "weekly"
            else:
                bucket_id = f"notebooklm-win-{win_code or idx}"
                display_name = f"Usage Window {win_code or idx}"
                window = str(win_code or idx)

            reset_dt: Optional[datetime] = None
            if isinstance(reset_arr, list) and reset_arr and isinstance(reset_arr[0], (int, float)):
                reset_dt = datetime.fromtimestamp(float(reset_arr[0]), tz=timezone.utc)

            remaining = clamp_fraction(to_decimal(pct_val) / Decimal("100"))
            buckets.append(
                QuotaBucket(
                    bucket_id=bucket_id,
                    display_name=display_name,
                    window=window,
                    remaining_fraction=remaining,
                    reset_time=reset_dt,
                )
            )

    plan_name: Optional[str] = None
    if tier_raw:
        if "ULTRA" in tier_raw:
            plan_name = "AI Ultra"
        elif "PRO" in tier_raw:
            plan_name = "AI Pro"
        elif "FREE" in tier_raw or "STANDARD" in tier_raw:
            plan_name = "Free"

    groups = (
        [QuotaGroup(group_id="notebooklm-limits", display_name="NotebookLM Usage", buckets=buckets)]
        if buckets
        else []
    )
    return ProviderUsageSnapshot(
        provider_id="notebooklm",
        provider_name="NotebookLM",
        source_label="Browser Session",
        plan_name=plan_name,
        groups=groups,
        error=None if buckets else "No usage windows returned by NotebookLM.",
    )


def extract_tier_from_ozz5z(ozz5z_data: Any) -> Optional[str]:
    """Recursively find NOTEBOOKLM_TIER_* string in ozz5Z payload."""
    if isinstance(ozz5z_data, str):
        if ozz5z_data.startswith("NOTEBOOKLM_TIER_"):
            return ozz5z_data
        return None
    if isinstance(ozz5z_data, list):
        for item in ozz5z_data:
            found = extract_tier_from_ozz5z(item)
            if found:
                return found
    return None


class NotebookLMProvider(UsageProvider):
    provider_id = "notebooklm"
    display_name = "NotebookLM"
    supports_oauth = False
    supports_token_tracking = False
    default_poll_interval_s = 300

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager

    def get_auth_status(self) -> AuthStatus:
        records, db_path = find_google_browser_cookies()
        if records and db_path:
            return AuthStatus(True, f"Browser cookies ({os.path.basename(os.path.dirname(os.path.dirname(db_path)))})")
        return AuthStatus(False, "No Google browser session cookies found")

    def fetch_usage(self) -> ProviderUsageSnapshot:
        records, _ = find_google_browser_cookies()
        if not records:
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="Browser Session",
                error="No Google browser cookies found. Sign in to notebook.google.com in your browser.",
            )

        session = build_google_session(records, origin=NOTEBOOK_BASE)

        try:
            page = session.get(f"{NOTEBOOK_BASE}/", timeout=12)
            if page.status_code >= 400:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Browser Session",
                    error=f"NotebookLM page load failed ({page.status_code}).",
                )
            m_at = re.search(r'"SNlM0e":"([^"]+)"', page.text)
            if not m_at:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Browser Session",
                    error="NotebookLM session not signed in (missing SNlM0e).",
                )
            at_token = m_at.group(1)
            m_bl = re.search(r'"cfb2h":"([^"]+)"', page.text)

            f_req_eyldcb = json.dumps(
                [[["EylDcb", json.dumps([[2, None, [1], [1, None, None, None, None, None, None, None, None, None, [1, 3]]]]), None, "generic"]]]
            )
            params: dict[str, str] = {"rpcids": "EylDcb", "source-path": "/", "rt": "c"}
            if m_bl:
                params["bl"] = m_bl.group(1)

            resp = session.post(BATCHEXECUTE_URL, params=params, data={"f.req": f_req_eyldcb, "at": at_token}, timeout=12)
            if resp.status_code >= 400:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Browser Session",
                    error=f"NotebookLM RPC error ({resp.status_code}).",
                )

            eyldcb_data = parse_batchexecute_rpc(resp.text, "EylDcb")
            if eyldcb_data is None:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Browser Session",
                    error="Could not parse NotebookLM EylDcb usage response.",
                )

            # Optional tier lookup via ozz5Z
            tier_raw: Optional[str] = None
            try:
                f_req_ozz = json.dumps(
                    [[["ozz5Z", json.dumps([[[None, "1", 1469], [None, None, None, None, None, None, None, None, None, [None, None, 2]], 1]]), None, "generic"]]]
                )
                params_ozz = {"rpcids": "ozz5Z", "source-path": "/", "rt": "c"}
                if m_bl:
                    params_ozz["bl"] = m_bl.group(1)
                r_ozz = session.post(BATCHEXECUTE_URL, params=params_ozz, data={"f.req": f_req_ozz, "at": at_token}, timeout=8)
                if r_ozz.status_code == 200:
                    tier_raw = extract_tier_from_ozz5z(parse_batchexecute_rpc(r_ozz.text, "ozz5Z"))
            except Exception:
                pass

            return parse_notebooklm_eyldcb(eyldcb_data, tier_raw=tier_raw)
        except Exception as exc:
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="Browser Session",
                error=f"NotebookLM request failed: {exc}",
            )
