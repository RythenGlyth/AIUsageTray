from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any


def default_config_dir() -> str:
    xdg = os.getenv("XDG_CONFIG_HOME")
    base = xdg if xdg else os.path.expanduser("~/.config")
    return os.path.join(base, "ai-usage-tray")


def default_data_dir() -> str:
    xdg = os.getenv("XDG_DATA_HOME")
    base = xdg if xdg else os.path.expanduser("~/.local/share")
    return os.path.join(base, "ai-usage-tray")


def default_runtime_dir() -> str:
    xdg = os.getenv("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "ai-usage-tray")
    return f"/tmp/ai-usage-tray-{os.getuid()}"


@dataclass
class ProviderConfig:
    enabled: bool = True
    mode: str = "auto"
    poll_interval_s: int = 300
    binary_path: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""


@dataclass
class AppConfig:
    tray_provider: str = "auto"
    tray_bucket_id: str = ""
    antigravity: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(enabled=True, mode="auto", poll_interval_s=60)
    )
    openai_codex: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(enabled=True, mode="auto", poll_interval_s=60)
    )
    claude: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(enabled=True, mode="auto", poll_interval_s=300)
    )
    notebooklm: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(enabled=True, mode="auto", poll_interval_s=300)
    )
    gemini_web: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(enabled=True, mode="auto", poll_interval_s=300)
    )
    github_copilot: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(enabled=True, mode="auto", poll_interval_s=300)
    )

    def get_provider(self, provider_id: str) -> ProviderConfig:
        mapping = {
            "antigravity": self.antigravity,
            "openai_codex": self.openai_codex,
            "claude": self.claude,
            "notebooklm": self.notebooklm,
            "gemini_web": self.gemini_web,
            "github_copilot": self.github_copilot,
        }
        return mapping.get(provider_id, ProviderConfig())


class ConfigManager:
    """Loads and persists AppConfig in XDG config directory."""

    def __init__(self, config_dir: str | None = None) -> None:
        self.config_dir = config_dir or default_config_dir()
        self.config_file = os.path.join(self.config_dir, "config.json")
        self.config = self.load()

    def load(self) -> AppConfig:
        cfg = AppConfig()
        if not os.path.exists(self.config_file):
            return cfg
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                raw: dict[str, Any] = json.load(f)
        except Exception:
            return cfg

        if isinstance(raw, dict):
            cfg.tray_provider = str(raw.get("tray_provider", cfg.tray_provider))
            cfg.tray_bucket_id = str(raw.get("tray_bucket_id", cfg.tray_bucket_id))

        for pid, target in (
            ("antigravity", cfg.antigravity),
            ("openai_codex", cfg.openai_codex),
            ("claude", cfg.claude),
            ("notebooklm", cfg.notebooklm),
            ("gemini_web", cfg.gemini_web),
            ("github_copilot", cfg.github_copilot),
        ):
            section = raw.get(pid)
            if isinstance(section, dict):
                target.enabled = bool(section.get("enabled", target.enabled))
                target.mode = str(section.get("mode", target.mode))
                target.poll_interval_s = max(15, int(section.get("poll_interval_s", target.poll_interval_s)))
                target.binary_path = str(section.get("binary_path", target.binary_path))
                target.oauth_client_id = str(section.get("oauth_client_id", target.oauth_client_id))
                target.oauth_client_secret = str(section.get("oauth_client_secret", target.oauth_client_secret))
        return cfg

    def save(self) -> None:
        os.makedirs(self.config_dir, mode=0o700, exist_ok=True)
        payload = {
            "tray_provider": self.config.tray_provider,
            "tray_bucket_id": self.config.tray_bucket_id,
            "antigravity": asdict(self.config.antigravity),
            "openai_codex": asdict(self.config.openai_codex),
            "claude": asdict(self.config.claude),
            "notebooklm": asdict(self.config.notebooklm),
            "gemini_web": asdict(self.config.gemini_web),
            "github_copilot": asdict(self.config.github_copilot),
        }
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.chmod(self.config_file, 0o600)
