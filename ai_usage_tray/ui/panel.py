from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QKeyEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ai_usage_tray.core.config import ConfigManager
from ai_usage_tray.core.models import ProviderUsageSnapshot, format_remaining_percent
from ai_usage_tray.core.provider import ProviderRegistry
from ai_usage_tray.core.storage import HistoryStorage
from ai_usage_tray.ui.analytics import AnalyticsTabView
from ai_usage_tray.ui.widgets import QuotaBucketCard, RingGaugeWidget


class ProviderTabView(QWidget):
    """Scrollable tab content for a single AI provider."""

    def __init__(
        self,
        provider_id: str,
        storage: HistoryStorage,
        get_trend_hours: Callable[[], int],
        on_connect_oauth: Callable[[str], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.provider_id = provider_id
        self.storage = storage
        self.get_trend_hours = get_trend_hours
        self.on_connect_oauth = on_connect_oauth
        self._last_snapshot: Optional[ProviderUsageSnapshot] = None
        self._disabled: bool = False

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 8, 6, 6)
        root.setSpacing(8)

        summary_frame = QFrame()
        summary_frame.setStyleSheet(
            """
            QFrame {
                background-color: rgba(128, 128, 128, 0.08);
                border-radius: 8px;
            }
            """
        )
        summary_layout = QHBoxLayout(summary_frame)
        summary_layout.setContentsMargins(10, 8, 10, 8)
        summary_layout.setSpacing(10)

        self.ring = RingGaugeWidget(size=54)
        summary_layout.addWidget(self.ring)

        meta_col = QVBoxLayout()
        meta_col.setSpacing(2)
        self.source_label = QLabel("Waiting for data...")
        src_font = QFont(self.source_label.font())
        src_font.setBold(True)
        src_font.setPointSize(9)
        self.source_label.setFont(src_font)
        meta_col.addWidget(self.source_label)

        self.badges_label = QLabel("")
        badge_font = QFont(self.badges_label.font())
        badge_font.setPointSize(8)
        self.badges_label.setFont(badge_font)
        self.badges_label.setStyleSheet("color: rgba(160, 160, 160, 0.95);")
        self.badges_label.setWordWrap(True)
        meta_col.addWidget(self.badges_label)

        summary_layout.addLayout(meta_col, 1)
        root.addWidget(summary_frame)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_body = QWidget()
        self.body_layout = QVBoxLayout(self.scroll_body)
        self.body_layout.setContentsMargins(0, 0, 4, 0)
        self.body_layout.setSpacing(8)
        self.body_layout.addStretch(1)
        self.scroll.setWidget(self.scroll_body)
        root.addWidget(self.scroll, 1)

    def _clear_body(self) -> None:
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def set_disabled_state(self, disabled: bool) -> None:
        self._disabled = disabled
        if disabled:
            self._clear_body()
            self.source_label.setText("Provider disabled")
            self.badges_label.setText("Enable in Settings to resume polling.")
            self.ring.set_value(None, "Off")
            msg = QLabel("This provider is disabled in Settings.")
            msg.setStyleSheet("color: rgba(150, 150, 150, 0.85); padding: 12px;")
            self.body_layout.addWidget(msg)
            self.body_layout.addStretch(1)
        elif self._last_snapshot is not None:
            self.update_snapshot(self._last_snapshot)

    def rerender_trends(self) -> None:
        if not self._disabled and self._last_snapshot is not None:
            self.update_snapshot(self._last_snapshot)

    def update_snapshot(self, snap: ProviderUsageSnapshot) -> None:
        self._last_snapshot = snap
        if self._disabled:
            return
        self._clear_body()

        meta_parts = [f"Source: {snap.source_label}"]
        if snap.plan_name:
            meta_parts.append(f"Plan: {snap.plan_name}")
        self.source_label.setText("  •  ".join(meta_parts))

        badge_parts = list(snap.badges)
        local_time = snap.fetched_at.astimezone().strftime("%H:%M:%S")
        badge_parts.append(f"Updated {local_time}")
        self.badges_label.setText("  •  ".join(badge_parts))

        lowest = snap.lowest_remaining_fraction
        if lowest is not None:
            short_pct = f"{int(round(float(lowest) * 100))}%"
            self.ring.set_value(float(lowest), short_pct)
            self.ring.setToolTip(f"Lowest remaining bucket: {format_remaining_percent(lowest)}")
        else:
            self.ring.set_value(None, "—")

        if snap.error:
            err_label = QLabel(snap.error)
            err_label.setWordWrap(True)
            err_label.setStyleSheet(
                "color: #e74c3c; background-color: rgba(231, 76, 60, 0.10); padding: 8px; border-radius: 6px;"
            )
            self.body_layout.addWidget(err_label)

            if snap.auth_required:
                connect_btn = QPushButton("Connect OAuth Account")
                connect_btn.clicked.connect(lambda: self.on_connect_oauth(self.provider_id))
                self.body_layout.addWidget(connect_btn)

        if not snap.groups and not snap.error:
            empty_lbl = QLabel("No active usage limit windows reported for this account.")
            empty_lbl.setWordWrap(True)
            empty_lbl.setStyleSheet("color: rgba(160, 160, 160, 0.95); padding: 8px;")
            self.body_layout.addWidget(empty_lbl)

        since = datetime.now(timezone.utc) - timedelta(hours=self.get_trend_hours())
        for group in snap.groups:
            group_header = QLabel(group.display_name)
            g_font = QFont(group_header.font())
            g_font.setBold(True)
            g_font.setPointSize(9)
            group_header.setFont(g_font)
            if group.description:
                group_header.setToolTip(group.description)
            self.body_layout.addWidget(group_header)

            if group.description:
                desc_label = QLabel(group.description)
                desc_font = QFont(desc_label.font())
                desc_font.setPointSize(8)
                desc_label.setFont(desc_font)
                desc_label.setWordWrap(True)
                desc_label.setStyleSheet("color: rgba(160, 160, 160, 0.90);")
                self.body_layout.addWidget(desc_label)

            for bucket in group.buckets:
                hist = self.storage.get_bucket_history_since(self.provider_id, bucket.bucket_id, since)
                card = QuotaBucketCard(bucket, history=hist)
                self.body_layout.addWidget(card)

        self.body_layout.addStretch(1)


class UsagePopupPanel(QWidget):
    """Resizable KDE Plasma window with reorderable provider tabs and separate Analytics view."""

    def __init__(
        self,
        config_manager: ConfigManager,
        registry: ProviderRegistry,
        storage: HistoryStorage,
        on_refresh_all: Callable[[], None],
        on_open_settings: Callable[[], None],
        on_connect_oauth: Callable[[str], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.registry = registry
        self.storage = storage
        self.on_refresh_all = on_refresh_all
        self.on_open_settings = on_open_settings
        self.on_connect_oauth = on_connect_oauth

        self.setWindowTitle("AI Usage Limits")
        self.setMinimumSize(440, 500)
        self.resize(500, 720)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        container = QFrame()
        container.setObjectName("MainPanel")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Header
        header = QHBoxLayout()
        title = QLabel("AI Usage Limits")
        t_font = QFont(title.font())
        t_font.setBold(True)
        t_font.setPointSize(11)
        title.setFont(t_font)
        header.addWidget(title, 1)

        self.status_label = QLabel("")
        s_font = QFont(self.status_label.font())
        s_font.setPointSize(8)
        self.status_label.setFont(s_font)
        self.status_label.setStyleSheet("color: rgba(160, 160, 160, 0.9);")
        header.addWidget(self.status_label)

        self.trend_combo = QComboBox()
        self.trend_combo.setToolTip("Sparkline & forecast lookback window")
        for label, hrs in (("1h", 1), ("1d", 24), ("7d", 168), ("30d", 720)):
            self.trend_combo.addItem(f"Trend: {label}", hrs)
        idx = self.trend_combo.findData(self.config_manager.config.trend_window_hours)
        self.trend_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self.trend_combo.currentIndexChanged.connect(self._on_trend_changed)
        header.addWidget(self.trend_combo)

        self.analytics_btn = QPushButton("📊")
        self.analytics_btn.setCheckable(True)
        self.analytics_btn.setToolTip("Toggle Analytics view")
        self.analytics_btn.setFixedSize(28, 26)
        self.analytics_btn.toggled.connect(self._on_analytics_toggled)
        header.addWidget(self.analytics_btn)

        refresh_btn = QPushButton("↻")
        refresh_btn.setToolTip("Refresh all providers")
        refresh_btn.setFixedSize(28, 26)
        refresh_btn.clicked.connect(self.on_refresh_all)
        header.addWidget(refresh_btn)

        settings_btn = QPushButton("⚙")
        settings_btn.setToolTip("Settings & Accounts")
        settings_btn.setFixedSize(28, 26)
        settings_btn.clicked.connect(self.on_open_settings)
        header.addWidget(settings_btn)

        layout.addLayout(header)

        # Stacked widget: 0 = Provider Tabs, 1 = Analytics Dashboard
        self.stack = QStackedWidget()

        self.tabs = QTabWidget()
        self.tabs.setMovable(True)
        self.tab_views: dict[str, ProviderTabView] = {}

        ordered_providers = self._ordered_providers()
        for provider in ordered_providers:
            view = ProviderTabView(
                provider_id=provider.provider_id,
                storage=self.storage,
                get_trend_hours=self._current_trend_hours,
                on_connect_oauth=self.on_connect_oauth,
            )
            self.tab_views[provider.provider_id] = view
            self.tabs.addTab(view, provider.display_name)

        self.tabs.tabBar().tabMoved.connect(self._on_tab_moved)
        self.stack.addWidget(self.tabs)

        self.analytics_view = AnalyticsTabView(storage=self.storage)
        self.stack.addWidget(self.analytics_view)

        layout.addWidget(self.stack, 1)
        outer.addWidget(container)

        self.refresh_provider_states()

    def _ordered_providers(self):
        all_providers = {p.provider_id: p for p in self.registry.all()}
        order = self.config_manager.config.tab_order
        ordered = [all_providers[pid] for pid in order if pid in all_providers]
        for p in self.registry.all():
            if p not in ordered:
                ordered.append(p)
        return ordered

    def _on_tab_moved(self, _from_idx: int, _to_idx: int) -> None:
        new_order: list[str] = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, ProviderTabView):
                new_order.append(w.provider_id)
        self.config_manager.config.tab_order = new_order
        self.config_manager.save()

    def refresh_provider_states(self) -> None:
        tab_bar = self.tabs.tabBar()
        default_color = self.palette().text().color()
        disabled_color = QColor(130, 130, 130, 130)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if not isinstance(w, ProviderTabView):
                continue
            enabled = self.config_manager.config.get_provider(w.provider_id).enabled
            w.set_disabled_state(not enabled)
            tab_bar.setTabTextColor(i, default_color if enabled else disabled_color)

    def _current_trend_hours(self) -> int:
        return int(self.trend_combo.currentData() or 24)

    def _on_trend_changed(self) -> None:
        hrs = self._current_trend_hours()
        self.config_manager.config.trend_window_hours = hrs
        self.config_manager.save()
        for view in self.tab_views.values():
            view.rerender_trends()

    def _on_analytics_toggled(self, checked: bool) -> None:
        if checked:
            self.analytics_view.refresh()
            self.stack.setCurrentWidget(self.analytics_view)
        else:
            self.stack.setCurrentWidget(self.tabs)

    def update_snapshot(self, snapshot: ProviderUsageSnapshot) -> None:
        view = self.tab_views.get(snapshot.provider_id)
        if view is not None:
            view.update_snapshot(snapshot)
        if self.stack.currentWidget() is self.analytics_view:
            self.analytics_view.refresh()
        self.status_label.setText(datetime.now(timezone.utc).astimezone().strftime("Last sync %H:%M:%S"))

    def set_refreshing(self, provider_id: str) -> None:
        self.status_label.setText(f"Refreshing {provider_id}...")

    def show_window(self) -> None:
        if self.stack.currentWidget() is self.analytics_view:
            self.analytics_view.refresh()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
