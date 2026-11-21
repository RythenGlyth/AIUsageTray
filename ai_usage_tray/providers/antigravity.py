from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import requests
import urllib3

from ai_usage_tray.core.auth import (
    delete_keyring_tokens,
    load_keyring_tokens,
    refresh_oauth_token,
    run_pkce_loopback_flow,
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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
CLOUDCODE_BASE = "https://cloudcode-pa.googleapis.com/v1internal"


@dataclass(slots=True)
class LocalLanguageServerInfo:
    pid: int
    csrf_token: str
    explicit_port: Optional[int] = None


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_server_info(pid: int, cmd: str) -> Optional[LocalLanguageServerInfo]:
    if "language_server" not in cmd:
        return None
    if "antigravity" not in cmd.lower() and "--csrf_token" not in cmd:
        return None
    token_match = re.search(r"--csrf_token(?:=|\s+)([A-Za-z0-9._-]+)", cmd)
    if not token_match:
        return None
    port_match = re.search(r"--https_server_port(?:=|\s+)(\d+)", cmd)
    explicit_port: Optional[int] = None
    if port_match:
        p = int(port_match.group(1))
        if p > 0:
            explicit_port = p
    return LocalLanguageServerInfo(
        pid=pid,
        csrf_token=token_match.group(1),
        explicit_port=explicit_port,
    )


def parse_ps_aux_for_antigravity(ps_output: str) -> list[LocalLanguageServerInfo]:
    """Parse `ps aux` output for running Antigravity language_server processes."""
    results: list[LocalLanguageServerInfo] = []
    for line in ps_output.splitlines():
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        info = _extract_server_info(pid, parts[10])
        if info:
            results.append(info)
    return results


def parse_ps_command_lines_for_antigravity(ps_output: str) -> list[LocalLanguageServerInfo]:
    """Parse `ps -ax -o pid=,command=` output (macOS/POSIX) for Antigravity language_server."""
    results: list[LocalLanguageServerInfo] = []
    for line in ps_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        info = _extract_server_info(pid, parts[1])
        if info:
            results.append(info)
    return results


def parse_windows_cim_processes_for_antigravity(json_output: str) -> list[LocalLanguageServerInfo]:
    """Parse PowerShell `Get-CimInstance Win32_Process | ConvertTo-Json` output."""
    if not json_output.strip():
        return []
    try:
        raw = json.loads(json_output)
    except Exception:
        return []
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    results: list[LocalLanguageServerInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pid_val = item.get("ProcessId")
        cmd = item.get("CommandLine") or ""
        if pid_val is None or not isinstance(cmd, str):
            continue
        try:
            pid = int(pid_val)
        except (ValueError, TypeError):
            continue
        info = _extract_server_info(pid, cmd)
        if info:
            results.append(info)
    return results


def parse_ss_ports_for_pid(ss_output: str, pid: int) -> list[int]:
    """Extract listening loopback TCP ports for a given PID from `ss -tlpn` output."""
    ports: list[int] = []
    pid_marker = f"pid={pid},"
    for line in ss_output.splitlines():
        if pid_marker not in line and f"pid={pid})" not in line:
            continue
        for token in line.split():
            if token.startswith(("127.0.0.1:", "[::1]:", "0.0.0.0:")):
                port_str = token.rsplit(":", 1)[-1]
                if port_str.isdigit():
                    port = int(port_str)
                    if port not in ports:
                        ports.append(port)
    return ports


def parse_lsof_ports_for_pid(lsof_output: str) -> list[int]:
    """Extract listening loopback TCP ports from `lsof -nP -iTCP -sTCP:LISTEN -a -p <pid>`."""
    ports: list[int] = []
    for line in lsof_output.splitlines():
        if "(LISTEN)" not in line:
            continue
        for token in line.split():
            if token.startswith(("127.0.0.1:", "[::1]:", "*:")):
                port_str = token.rsplit(":", 1)[-1]
                if port_str.isdigit():
                    port = int(port_str)
                    if port not in ports:
                        ports.append(port)
    return ports


def parse_windows_netstat_ports_for_pid(netstat_output: str, pid: int) -> list[int]:
    """Extract listening loopback TCP ports for a given PID from `netstat -ano -p tcp`."""
    ports: list[int] = []
    pid_str = str(pid)
    for line in netstat_output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[-1] != pid_str or parts[-2].upper() != "LISTENING":
            continue
        local_addr = parts[1]
        if local_addr.startswith(("127.0.0.1:", "[::1]:", "0.0.0.0:", "[::]:")):
            port_str = local_addr.rsplit(":", 1)[-1]
            if port_str.isdigit():
                port = int(port_str)
                if port not in ports:
                    ports.append(port)
    return ports


def _discover_windows_servers() -> list[tuple[LocalLanguageServerInfo, list[int]]]:
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name LIKE '%language_server%'\" "
        "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=6,
        )
        if proc.returncode != 0:
            return []
        servers = parse_windows_cim_processes_for_antigravity(proc.stdout)
    except Exception:
        return []

    if not servers:
        return []

    netstat_output = ""
    try:
        ns_proc = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=5)
        if ns_proc.returncode == 0:
            netstat_output = ns_proc.stdout
    except Exception:
        pass

    discovered: list[tuple[LocalLanguageServerInfo, list[int]]] = []
    for srv in servers:
        ports: list[int] = [srv.explicit_port] if srv.explicit_port else []
        for p in parse_windows_netstat_ports_for_pid(netstat_output, srv.pid):
            if p not in ports:
                ports.append(p)
        if ports:
            discovered.append((srv, ports))
    return discovered


def _discover_macos_servers() -> list[tuple[LocalLanguageServerInfo, list[int]]]:
    try:
        ps_proc = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True, timeout=5)
        if ps_proc.returncode != 0:
            return []
        servers = parse_ps_command_lines_for_antigravity(ps_proc.stdout)
    except Exception:
        return []

    discovered: list[tuple[LocalLanguageServerInfo, list[int]]] = []
    for srv in servers:
        ports: list[int] = [srv.explicit_port] if srv.explicit_port else []
        try:
            lsof_proc = subprocess.run(
                ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(srv.pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if lsof_proc.returncode == 0:
                for p in parse_lsof_ports_for_pid(lsof_proc.stdout):
                    if p not in ports:
                        ports.append(p)
        except Exception:
            pass
        if ports:
            discovered.append((srv, ports))
    return discovered


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def discover_antigravity_hub_daemons(
    daemon_dir: Optional[str] = None,
) -> list[tuple[LocalLanguageServerInfo, list[int]]]:
    """Discover running Antigravity VS Code extension `agy --hub` daemons via `~/.gemini/antigravity/daemon/hub_*.json`."""
    target_dir = daemon_dir or os.path.expanduser("~/.gemini/antigravity/daemon")
    if not os.path.isdir(target_dir):
        return []

    hub_files = glob.glob(os.path.join(target_dir, "hub_*.json"))
    hub_files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0.0, reverse=True)

    discovered: list[tuple[LocalLanguageServerInfo, list[int]]] = []
    for path in hub_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        csrf_token = data.get("csrfToken")
        pid_val = data.get("pid")
        if not isinstance(csrf_token, str) or not csrf_token or pid_val is None:
            continue
        try:
            pid = int(pid_val)
        except (ValueError, TypeError):
            continue
        if not _is_pid_alive(pid):
            continue

        ports: list[int] = []
        for key in ("httpsPort", "httpPort"):
            port_val = data.get(key)
            if isinstance(port_val, int) and port_val > 0 and port_val not in ports:
                ports.append(port_val)
        if not ports:
            continue

        discovered.append(
            (LocalLanguageServerInfo(pid=pid, csrf_token=csrf_token, explicit_port=ports[0]), ports)
        )
    return discovered


def _discover_process_language_servers() -> list[tuple[LocalLanguageServerInfo, list[int]]]:
    if sys.platform == "win32":
        return _discover_windows_servers()
    if sys.platform == "darwin":
        return _discover_macos_servers()

    try:
        ps_proc = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        if ps_proc.returncode != 0:
            return []
        servers = parse_ps_aux_for_antigravity(ps_proc.stdout)
    except Exception:
        return []

    if not servers:
        return []

    ss_output = ""
    try:
        ss_proc = subprocess.run(["ss", "-tlpn"], capture_output=True, text=True, timeout=5)
        if ss_proc.returncode == 0:
            ss_output = ss_proc.stdout
    except Exception:
        pass

    discovered: list[tuple[LocalLanguageServerInfo, list[int]]] = []
    for srv in servers:
        ports: list[int] = []
        if srv.explicit_port:
            ports.append(srv.explicit_port)
        for p in parse_ss_ports_for_pid(ss_output, srv.pid):
            if p not in ports:
                ports.append(p)
        if ports:
            discovered.append((srv, ports))
    return discovered


def discover_local_language_servers() -> list[tuple[LocalLanguageServerInfo, list[int]]]:
    """Discover running Antigravity language_server and VS Code extension `agy --hub` instances."""
    combined: list[tuple[LocalLanguageServerInfo, list[int]]] = []
    by_pid: dict[int, tuple[LocalLanguageServerInfo, list[int]]] = {}

    for srv, ports in [*_discover_process_language_servers(), *discover_antigravity_hub_daemons()]:
        if srv.pid in by_pid:
            existing_ports = by_pid[srv.pid][1]
            for p in ports:
                if p not in existing_ports:
                    existing_ports.append(p)
        else:
            entry = (srv, list(ports))
            by_pid[srv.pid] = entry
            combined.append(entry)

    return combined


def parse_retrieve_user_quota_summary(payload: dict[str, Any]) -> ProviderUsageSnapshot:
    """Parse `/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary` JSON."""
    response = payload.get("response", payload)
    raw_groups = response.get("groups", []) if isinstance(response, dict) else []

    groups: list[QuotaGroup] = []
    for idx, g in enumerate(raw_groups):
        if not isinstance(g, dict):
            continue
        g_name = str(g.get("displayName", f"Group {idx + 1}"))
        g_desc = str(g.get("description", ""))
        buckets: list[QuotaBucket] = []
        for b in g.get("buckets", []):
            if not isinstance(b, dict):
                continue
            raw_frac = b.get("remainingFraction", 0)
            frac = clamp_fraction(to_decimal(raw_frac))
            buckets.append(
                QuotaBucket(
                    bucket_id=str(b.get("bucketId", b.get("displayName", "unknown"))),
                    display_name=str(b.get("displayName", "Quota")),
                    window=str(b.get("window", "")),
                    remaining_fraction=frac,
                    reset_time=parse_iso_datetime(b.get("resetTime")),
                    description=str(b.get("description", "")),
                )
            )
        groups.append(
            QuotaGroup(
                group_id=f"ag-group-{idx}",
                display_name=g_name,
                description=g_desc,
                buckets=buckets,
            )
        )

    return ProviderUsageSnapshot(
        provider_id="antigravity",
        provider_name="Antigravity",
        source_label="Local Language Server",
        groups=groups,
        raw_metadata={"summaryDescription": response.get("description", "")} if isinstance(response, dict) else {},
    )


class AntigravityProvider(UsageProvider):
    provider_id = "antigravity"
    display_name = "Antigravity"
    supports_oauth = True
    supports_token_tracking = False
    default_poll_interval_s = 60

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager

    def get_auth_status(self) -> AuthStatus:
        mode = self.config_manager.config.antigravity.mode
        local_servers = discover_local_language_servers() if mode in ("auto", "local_only") else []
        tokens = load_keyring_tokens(self.provider_id)

        if local_servers and mode != "oauth_only":
            return AuthStatus(True, f"Local language server (PID {local_servers[0][0].pid})")
        if tokens is not None and mode != "local_only":
            return AuthStatus(True, "Google OAuth (keyring)")
        if mode == "local_only":
            return AuthStatus(False, "No running Antigravity language server found")
        return AuthStatus(False, "No local server running and no OAuth token configured", needs_interactive_login=True)

    def start_oauth_login(self) -> tuple[bool, str]:
        cfg = self.config_manager.config.antigravity
        if not cfg.oauth_client_id:
            return (
                False,
                "Set a Google OAuth Client ID in Settings first (or use Local Language Server mode without auth).",
            )
        tokens, msg = run_pkce_loopback_flow(
            auth_url=GOOGLE_AUTH_URL,
            token_url=GOOGLE_TOKEN_URL,
            client_id=cfg.oauth_client_id,
            client_secret=cfg.oauth_client_secret,
            scopes=GOOGLE_SCOPES,
            extra_auth_params={"access_type": "offline", "prompt": "consent"},
        )
        if tokens is None:
            return False, msg
        save_keyring_tokens(self.provider_id, tokens)
        return True, "Connected to Antigravity OAuth."

    def disconnect_oauth(self) -> None:
        delete_keyring_tokens(self.provider_id)

    def fetch_usage(self) -> ProviderUsageSnapshot:
        mode = self.config_manager.config.antigravity.mode

        if mode in ("auto", "local_only"):
            local_snap = self._fetch_from_local_ls()
            if local_snap is not None:
                return local_snap
            if mode == "local_only":
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="Local Language Server",
                    error="Antigravity language_server is not running.",
                )

        if mode in ("auto", "oauth_only"):
            return self._fetch_from_oauth()

        return ProviderUsageSnapshot(
            provider_id=self.provider_id,
            provider_name=self.display_name,
            source_label="Unconfigured",
            error=f"Unknown mode: {mode}",
        )

    def _fetch_from_local_ls(self) -> Optional[ProviderUsageSnapshot]:
        servers = discover_local_language_servers()
        for srv, ports in servers:
            headers = {
                "Content-Type": "application/json",
                "x-codeium-csrf-token": srv.csrf_token,
            }
            for port in ports:
                for scheme in ("https", "http"):
                    url = (
                        f"{scheme}://127.0.0.1:{port}"
                        "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
                    )
                    try:
                        resp = requests.post(url, json={}, headers=headers, timeout=3, verify=False)
                        if resp.status_code == 200:
                            data = resp.json()
                            if isinstance(data, dict) and ("response" in data or "groups" in data):
                                return parse_retrieve_user_quota_summary(data)
                    except Exception:
                        continue
        return None

    def _fetch_from_oauth(self) -> ProviderUsageSnapshot:
        cfg = self.config_manager.config.antigravity
        tokens = load_keyring_tokens(self.provider_id)
        if tokens is None:
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="OAuth",
                auth_required=True,
                error="Antigravity local server not found and OAuth is not connected.",
            )

        if tokens.is_expired() and tokens.refresh_token and cfg.oauth_client_id:
            refreshed = refresh_oauth_token(
                GOOGLE_TOKEN_URL,
                cfg.oauth_client_id,
                tokens.refresh_token,
                client_secret=cfg.oauth_client_secret,
            )
            if refreshed is not None:
                tokens = refreshed
                save_keyring_tokens(self.provider_id, tokens)

        headers = {
            "Authorization": f"Bearer {tokens.access_token}",
            "Content-Type": "application/json",
        }
        try:
            load_resp = requests.post(
                f"{CLOUDCODE_BASE}:loadCodeAssist",
                json={"metadata": {"ideType": "ANTIGRAVITY"}},
                headers=headers,
                timeout=10,
            )
            plan_name: Optional[str] = None
            project_id: Optional[str] = None
            if load_resp.status_code == 200:
                load_data = load_resp.json()
                project_id = load_data.get("cloudaicompanionProject")
                tier = load_data.get("currentTier") or load_data.get("paidTier") or {}
                if isinstance(tier, dict):
                    plan_name = tier.get("name") or tier.get("id")

            body: dict[str, Any] = {}
            if project_id:
                body["project"] = project_id
            models_resp = requests.post(
                f"{CLOUDCODE_BASE}:fetchAvailableModels",
                json=body,
                headers=headers,
                timeout=10,
            )
            if models_resp.status_code == 401:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="OAuth",
                    auth_required=True,
                    error="Google OAuth token expired or unauthorized. Please reconnect.",
                )
            if models_resp.status_code >= 400:
                return ProviderUsageSnapshot(
                    provider_id=self.provider_id,
                    provider_name=self.display_name,
                    source_label="OAuth",
                    error=f"Cloud Code API error ({models_resp.status_code}): {models_resp.text[:200]}",
                )

            data = models_resp.json()
            models_map = data.get("models", {}) if isinstance(data, dict) else {}
            buckets: list[QuotaBucket] = []
            for model_id, info in models_map.items():
                if not isinstance(info, dict):
                    continue
                qinfo = info.get("quotaInfo")
                if not isinstance(qinfo, dict) or "remainingFraction" not in qinfo:
                    continue
                buckets.append(
                    QuotaBucket(
                        bucket_id=str(model_id),
                        display_name=str(info.get("displayName", model_id)),
                        window="quota",
                        remaining_fraction=clamp_fraction(to_decimal(qinfo.get("remainingFraction"))),
                        reset_time=parse_iso_datetime(qinfo.get("resetTime")),
                    )
                )

            groups = (
                [QuotaGroup(group_id="oauth-models", display_name="Model Quotas", buckets=buckets)]
                if buckets
                else []
            )
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="OAuth",
                plan_name=plan_name,
                groups=groups,
                error=None if buckets else "No quotaInfo returned by fetchAvailableModels.",
            )
        except Exception as exc:
            return ProviderUsageSnapshot(
                provider_id=self.provider_id,
                provider_name=self.display_name,
                source_label="OAuth",
                error=f"Antigravity OAuth request failed: {exc}",
            )
