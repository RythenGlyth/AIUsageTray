from __future__ import annotations

import glob
import json
import os
import re
from typing import Any, Optional

import requests

from ai_usage_tray.core.chromium_crypto import ChromiumCookie, read_chromium_cookie_records

BROWSER_COOKIE_PATTERNS = (
    ("~/.config/yandex-browser/*/Cookies", ("yandex", "Yandex", "yandex-browser", "chrome", "chromium")),
    ("~/.config/google-chrome/*/Cookies", ("chrome", "chromium")),
    ("~/.config/chromium/*/Cookies", ("chromium", "chrome")),
    ("~/.config/BraveSoftware/Brave-Browser/*/Cookies", ("brave", "Brave", "chrome", "chromium")),
    ("~/.config/vivaldi/*/Cookies", ("vivaldi", "Vivaldi", "chrome", "chromium")),
)


def find_google_browser_cookies() -> tuple[list[ChromiumCookie], Optional[str]]:
    """Find Google session cookies from installed Chromium-based browsers."""
    for pattern, apps in BROWSER_COOKIE_PATTERNS:
        for db_path in sorted(glob.glob(os.path.expanduser(pattern))):
            records = read_chromium_cookie_records(db_path, "google.com", applications=apps)
            names = {r.name for r in records}
            if "SID" in names or "__Secure-1PSID" in names:
                return records, db_path
    return [], None


def build_google_session(records: list[ChromiumCookie], origin: str) -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    session.headers["Origin"] = origin
    for c in records:
        session.cookies.set(c.name, c.value, domain=c.host_key, path=c.path, secure=c.is_secure)
    return session


def parse_batchexecute_rpc(response_text: str, rpc_id: str) -> Optional[Any]:
    """Extract and JSON-decode the payload for a specific rpc_id from batchexecute output."""
    for line in response_text.splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            outer = json.loads(line)
        except Exception:
            continue
        if not isinstance(outer, list):
            continue
        for item in outer:
            if (
                isinstance(item, list)
                and len(item) >= 3
                and item[0] == "wrb.fr"
                and item[1] == rpc_id
                and isinstance(item[2], str)
            ):
                try:
                    return json.loads(item[2])
                except Exception:
                    return None
    return None


def discover_rpcid_from_html(
    session: requests.Session,
    html_text: str,
    method_names: tuple[str, ...],
    max_scripts: int = 8,
) -> Optional[str]:
    """Scan linked Wiz/Boq JS bundles for a batchexecute RPC method name -> 5-char rpcid mapping."""
    script_urls = re.findall(r'src="(https://www\.gstatic\.com/_/[^"]+\.js[^"]*)"', html_text)
    if not script_urls:
        return None

    for url in script_urls[:max_scripts]:
        try:
            resp = session.get(url, timeout=8)
            if resp.status_code != 200:
                continue
            js = resp.text
            for method in method_names:
                if method not in js:
                    continue
                # Typical Wiz/Boq patterns: new _.Xx("qpEbW", ... "CheckGeminiQuota") or "qpEbW",...CheckGeminiQuota
                m = re.search(
                    rf'["\']([A-Za-z0-9]{{5,6}})["\'][^;]{{0,120}}{re.escape(method)}|{re.escape(method)}[^;]{{0,120}}["\']([A-Za-z0-9]{{5,6}})["\']',
                    js,
                )
                if m:
                    return m.group(1) or m.group(2)
        except Exception:
            continue
    return None
