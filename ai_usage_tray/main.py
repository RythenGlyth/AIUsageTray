from __future__ import annotations

import atexit
import fcntl
import os
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication

from ai_usage_tray.core.config import ConfigManager, default_runtime_dir
from ai_usage_tray.core.provider import ProviderRegistry
from ai_usage_tray.core.storage import HistoryStorage
from ai_usage_tray.providers.antigravity import AntigravityProvider
from ai_usage_tray.providers.claude import ClaudeProvider
from ai_usage_tray.providers.gemini_web import GeminiWebProvider
from ai_usage_tray.providers.github_copilot import GitHubCopilotProvider
from ai_usage_tray.providers.notebooklm import NotebookLMProvider
from ai_usage_tray.providers.openai_codex import OpenAICodexProvider
from ai_usage_tray.ui.tray import AIUsageTrayApp


def acquire_single_instance_lock() -> Optional[object]:
    """Acquire an exclusive flock lock so only one tray instance runs per user."""
    runtime_dir = default_runtime_dir()
    os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
    lock_path = os.path.join(runtime_dir, "ai-usage-tray.lock")
    fh = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    atexit.register(lambda: fh.close())
    return fh


def main() -> int:
    lock_handle = acquire_single_instance_lock()
    if lock_handle is None:
        print("Another ai-usage-tray instance is already running.", file=sys.stderr, flush=True)
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName("AIUsageTray")
    app.setQuitOnLastWindowClosed(False)

    config_manager = ConfigManager()
    storage = HistoryStorage()

    registry = ProviderRegistry()
    registry.register(AntigravityProvider(config_manager))
    registry.register(OpenAICodexProvider(config_manager))
    registry.register(ClaudeProvider(config_manager))
    registry.register(NotebookLMProvider(config_manager))
    registry.register(GeminiWebProvider(config_manager))
    registry.register(GitHubCopilotProvider(config_manager))

    tray_app = AIUsageTrayApp(app, config_manager, registry, storage)
    # Keep strong reference for the lifetime of app.exec()
    app._tray_app = tray_app  # type: ignore[attr-defined]
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
