from __future__ import annotations

import threading
from typing import Optional

from PyQt6.QtCore import QPoint, QRectF, Qt
from PyQt6.QtGui import QAction, QColor, QCursor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon, QWidgetAction

from ai_usage_tray.core.config import ConfigManager
from ai_usage_tray.core.coordinator import RefreshCoordinator
from ai_usage_tray.core.models import ProviderUsageSnapshot, format_remaining_percent
from ai_usage_tray.core.provider import ProviderRegistry
from ai_usage_tray.core.storage import HistoryStorage
from ai_usage_tray.ui.panel import UsagePopupPanel
from ai_usage_tray.ui.settings_dialog import SettingsDialog
from ai_usage_tray.ui.widgets import status_color_for_fraction


def build_tray_icon(lowest_fraction: Optional[float]) -> QIcon:
    """Render a crisp 64x64 KDE-friendly ring gauge tray icon."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    rect = QRectF(7, 7, size - 14, size - 14)
    track_pen = QPen(QColor(180, 180, 180, 90), 8.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(track_pen)
    painter.drawEllipse(rect)

    if lowest_fraction is not None:
        clamped = max(0.0, min(1.0, lowest_fraction))
        color = status_color_for_fraction(clamped)
        arc_pen = QPen(color, 8.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(arc_pen)
        painter.drawArc(rect, 90 * 16, int(-360.0 * clamped * 16.0))

        # Center dot
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(25, 25, 14, 14))
    else:
        painter.setBrush(QColor(160, 160, 160, 180))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(25, 25, 14, 14))

    painter.end()
    return QIcon(pixmap)


class AIUsageTrayApp:
    """Main KDE Plasma system tray controller."""

    def __init__(
        self,
        app: QApplication,
        config_manager: ConfigManager,
        registry: ProviderRegistry,
        storage: HistoryStorage,
    ) -> None:
        self.app = app
        self.config_manager = config_manager
        self.registry = registry
        self.storage = storage
        self.settings_dialog: Optional[SettingsDialog] = None

        self.coordinator = RefreshCoordinator(registry, config_manager, storage)
        self.panel = UsagePopupPanel(
            config_manager=config_manager,
            registry=registry,
            storage=storage,
            on_refresh_all=lambda: self.coordinator.refresh_all(force=True),
            on_open_settings=self.open_settings,
            on_connect_oauth=self._connect_oauth_from_panel,
        )

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(build_tray_icon(None))
        self.tray.setToolTip("AI Usage Limits — Loading...")
        self.tray.activated.connect(self._on_tray_activated)

        self.menu = QMenu()
        open_act = QAction("Open Usage Panel", self.menu)
        open_act.triggered.connect(self.toggle_panel)
        self.menu.addAction(open_act)

        refresh_act = QAction("Refresh All", self.menu)
        refresh_act.triggered.connect(lambda: self.coordinator.refresh_all(force=True))
        self.menu.addAction(refresh_act)

        settings_act = QAction("Settings...", self.menu)
        settings_act.triggered.connect(self.open_settings)
        self.menu.addAction(settings_act)

        self.menu.addSeparator()

        quit_act = QAction("Quit", self.menu)
        quit_act.triggered.connect(self.app.quit)
        self.menu.addAction(quit_act)

        self.tray.setContextMenu(self.menu)
        self.tray.show()

        self.coordinator.snapshot_updated.connect(self._on_snapshot_updated)
        self.coordinator.refresh_started.connect(self.panel.set_refreshing)
        self.coordinator.start()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_panel()

    def toggle_panel(self) -> None:
        if self.panel.isVisible():
            self.panel.hide()
        else:
            self.panel.show_window()

    def open_settings(self) -> None:
        if self.settings_dialog is not None:
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return
        dlg = SettingsDialog(
            config_manager=self.config_manager,
            registry=self.registry,
            on_settings_saved=self._on_settings_saved,
            latest_snapshots=self.coordinator.latest_snapshots(),
        )
        self.settings_dialog = dlg
        dlg.finished.connect(lambda _res: setattr(self, "settings_dialog", None))
        dlg.show()

    def _on_settings_saved(self) -> None:
        self.panel.refresh_provider_states()
        self._update_tray_summary()
        self.coordinator.reconfigure_timers()
        self.coordinator.refresh_all(force=True)

    def _connect_oauth_from_panel(self, provider_id: str) -> None:
        provider = self.registry.get(provider_id)
        if provider is None:
            return
        self.panel.set_refreshing(provider_id)

        def _worker() -> None:
            ok, msg = provider.start_oauth_login()
            if ok:
                self.coordinator.refresh_provider(provider_id, force=True)
            else:
                self.tray.showMessage("AIUsageTray OAuth", msg, QSystemTrayIcon.MessageIcon.Warning)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_snapshot_updated(self, snapshot: ProviderUsageSnapshot) -> None:
        self.panel.update_snapshot(snapshot)
        self._update_tray_summary()

    def _resolve_tray_fraction(self, snapshots: dict[str, ProviderUsageSnapshot]) -> Optional[float]:
        target_pid = self.config_manager.config.tray_provider
        target_bid = self.config_manager.config.tray_bucket_id

        if target_pid and target_pid != "auto":
            snap = snapshots.get(target_pid)
            if snap is None:
                return None
            if target_bid:
                for b in snap.all_buckets:
                    if b.bucket_id == target_bid:
                        return float(b.remaining_fraction)
            low = snap.lowest_remaining_fraction
            return float(low) if low is not None else None

        lowest_all: Optional[float] = None
        for provider in self.registry.all():
            snap = snapshots.get(provider.provider_id)
            if snap is None:
                continue
            low = snap.lowest_remaining_fraction
            if low is not None:
                low_f = float(low)
                if lowest_all is None or low_f < lowest_all:
                    lowest_all = low_f
        return lowest_all

    def _update_tray_summary(self) -> None:
        snapshots = self.coordinator.latest_snapshots()
        tray_fraction = self._resolve_tray_fraction(snapshots)
        tooltip_lines: list[str] = ["AI Usage Limits"]

        for provider in self.registry.all():
            snap = snapshots.get(provider.provider_id)
            if snap is None:
                continue
            low = snap.lowest_remaining_fraction
            if low is not None:
                tooltip_lines.append(f"{snap.provider_name}: {format_remaining_percent(low)} min remaining")
            elif snap.error:
                tooltip_lines.append(f"{snap.provider_name}: {snap.error[:60]}")

        self.tray.setIcon(build_tray_icon(tray_fraction))
        self.tray.setToolTip("\n".join(tooltip_lines))
