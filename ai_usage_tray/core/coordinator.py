from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from ai_usage_tray.core.config import ConfigManager
from ai_usage_tray.core.models import ProviderUsageSnapshot
from ai_usage_tray.core.provider import ProviderRegistry, UsageProvider
from ai_usage_tray.core.storage import HistoryStorage


class _FetchSignals(QObject):
    finished = pyqtSignal(object)


class _FetchTask(QRunnable):
    def __init__(self, provider: UsageProvider, signals: _FetchSignals) -> None:
        super().__init__()
        self.provider = provider
        self.signals = signals

    def run(self) -> None:
        try:
            snap = self.provider.fetch_usage()
        except Exception as exc:  # pragma: no cover - defensive guard
            snap = ProviderUsageSnapshot(
                provider_id=self.provider.provider_id,
                provider_name=self.provider.display_name,
                source_label="Error",
                error=str(exc),
            )
        self.signals.finished.emit(snap)


class RefreshCoordinator(QObject):
    """Coordinates background provider polling with per-provider locks and backoff."""

    snapshot_updated = pyqtSignal(object)
    refresh_started = pyqtSignal(str)
    all_snapshots_updated = pyqtSignal()

    def __init__(
        self,
        registry: ProviderRegistry,
        config_manager: ConfigManager,
        storage: HistoryStorage,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.registry = registry
        self.config_manager = config_manager
        self.storage = storage
        self.pool = QThreadPool.globalInstance()
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()
        self._snapshots: dict[str, ProviderUsageSnapshot] = {}
        self._timers: dict[str, QTimer] = {}
        self._signals_refs: list[_FetchSignals] = []

    def latest_snapshots(self) -> dict[str, ProviderUsageSnapshot]:
        return dict(self._snapshots)

    def start(self) -> None:
        self.reconfigure_timers()
        self.refresh_all(force=True)

    def reconfigure_timers(self) -> None:
        for timer in self._timers.values():
            timer.stop()
            timer.deleteLater()
        self._timers.clear()

        for provider in self.registry.all():
            pid = provider.provider_id
            pcfg = self.config_manager.config.get_provider(pid)
            if not pcfg.enabled:
                continue
            timer = QTimer(self)
            timer.setInterval(max(15, pcfg.poll_interval_s) * 1000)
            timer.timeout.connect(lambda p_id=pid: self.refresh_provider(p_id, force=False))
            timer.start()
            self._timers[pid] = timer

    def refresh_all(self, force: bool = True) -> None:
        for provider in self.registry.all():
            pcfg = self.config_manager.config.get_provider(provider.provider_id)
            if pcfg.enabled:
                self.refresh_provider(provider.provider_id, force=force)

    def refresh_provider(self, provider_id: str, force: bool = False) -> None:
        provider = self.registry.get(provider_id)
        if provider is None:
            return
        pcfg = self.config_manager.config.get_provider(provider_id)
        if not pcfg.enabled:
            return

        if not force:
            prev = self._snapshots.get(provider_id)
            if prev and prev.rate_limited_until:
                if datetime.now(timezone.utc) < prev.rate_limited_until:
                    return

        with self._lock:
            if provider_id in self._in_flight:
                return
            self._in_flight.add(provider_id)

        self.refresh_started.emit(provider_id)
        signals = _FetchSignals()
        self._signals_refs.append(signals)
        signals.finished.connect(lambda snap, s=signals: self._on_task_finished(snap, s))
        task = _FetchTask(provider, signals)
        self.pool.start(task)

    def _on_task_finished(self, snapshot: ProviderUsageSnapshot, signals: _FetchSignals) -> None:
        with self._lock:
            self._in_flight.discard(snapshot.provider_id)
        if signals in self._signals_refs:
            self._signals_refs.remove(signals)
        signals.deleteLater()

        self._snapshots[snapshot.provider_id] = snapshot
        self.storage.record_snapshot(snapshot)
        self.snapshot_updated.emit(snapshot)
        self.all_snapshots_updated.emit()
