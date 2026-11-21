from __future__ import annotations

import atexit
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
    """Acquire an exclusive non-blocking file lock so only one tray instance runs per user."""
    runtime_dir = default_runtime_dir()
    os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
    lock_path = os.path.join(runtime_dir, "ai-usage-tray.lock")
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass
    atexit.register(lambda: fh.close())
    return fh


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    capture_screenshots = "--capture-screenshots" in args
    demo_mode = "--demo" in args or capture_screenshots

    if not demo_mode:
        lock_handle = acquire_single_instance_lock()
        if lock_handle is None:
            print("Another ai-usage-tray instance is already running.", file=sys.stderr, flush=True)
            return 1

    app = QApplication(sys.argv)
    app.setApplicationName("AIUsageTray")
    app.setQuitOnLastWindowClosed(False)

    if demo_mode:
        import shutil
        import tempfile

        demo_dir = tempfile.mkdtemp(prefix="ai_usage_tray_demo_")
        atexit.register(lambda: shutil.rmtree(demo_dir, ignore_errors=True))
        config_manager = ConfigManager(config_dir=demo_dir)
        storage = HistoryStorage(db_path=os.path.join(demo_dir, "history.db"))
        registry = ProviderRegistry()
        from ai_usage_tray.demo import register_demo_providers

        register_demo_providers(registry, storage)
    else:
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
    if capture_screenshots:
        from PyQt6.QtCore import QTimer

        from ai_usage_tray.demo import capture_demo_screenshots

        def _run_capture() -> None:
            saved = capture_demo_screenshots(app, tray_app, output_dir="docs/images", scale=2.0)
            for path in saved:
                print(f"Saved screenshot: {path}", flush=True)
            app.quit()

        QTimer.singleShot(500, _run_capture)
    elif demo_mode:
        tray_app.panel.show_window()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
