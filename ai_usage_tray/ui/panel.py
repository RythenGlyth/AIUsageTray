from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QColor, QCursor, QFont, QKeyEvent
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ai_usage_tray.core.models import ProviderUsageSnapshot, format_remaining_percent
from ai_usage_tray.core.provider import ProviderRegistry
from ai_usage_tray.core.storage import HistoryStorage
from ai_usage_tray.ui.widgets import QuotaBucketCard, RingGaugeWidget


class ProviderTabView(QWidget):
    """Scrollable tab content for a single AI provider."""

    def __init__(
        self,
        provider_id: str,
        storage: HistoryStorage,
        on_connect_oauth: Callable[[str], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.provider_id = provider_id
        self.storage = storage
        self.on_connect_oauth = on_connect_oauth

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 8, 6, 6)
        root.setSpacing(8)

        # Summary header
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

        # Scroll area for quota groups/buckets
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

    def update_snapshot(self, snap: ProviderUsageSnapshot) -> None:
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
                hist = self.storage.get_bucket_history(self.provider_id, bucket.bucket_id, limit=24)
                card = QuotaBucketCard(bucket, history=hist)
                self.body_layout.addWidget(card)

        self.body_layout.addStretch(1)


class UsagePopupPanel(QWidget):
    """Resizable KDE Plasma window with provider usage tabs."""

    def __init__(
        self,
        registry: ProviderRegistry,
        storage: HistoryStorage,
        on_refresh_all: Callable[[], None],
        on_open_settings: Callable[[], None],
        on_connect_oauth: Callable[[str], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.registry = registry
        self.storage = storage
        self.on_refresh_all = on_refresh_all
        self.on_open_settings = on_open_settings
        self.on_connect_oauth = on_connect_oauth

        self.setWindowTitle("AI Usage Limits")
        self.setMinimumSize(420, 480)
        self.resize(480, 700)

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

        # Provider tabs
        self.tabs = QTabWidget()
        self.tab_views: dict[str, ProviderTabView] = {}
        for provider in self.registry.all():
            view = ProviderTabView(
                provider_id=provider.provider_id,
                storage=self.storage,
                on_connect_oauth=self.on_connect_oauth,
            )
            self.tab_views[provider.provider_id] = view
            self.tabs.addTab(view, provider.display_name)
        layout.addWidget(self.tabs, 1)

        outer.addWidget(container)

    def update_snapshot(self, snapshot: ProviderUsageSnapshot) -> None:
        view = self.tab_views.get(snapshot.provider_id)
        if view is not None:
            view.update_snapshot(snapshot)
        self.status_label.setText(datetime.now(timezone.utc).astimezone().strftime("Last sync %H:%M:%S"))

    def set_refreshing(self, provider_id: str) -> None:
        self.status_label.setText(f"Refreshing {provider_id}...")

    def show_window(self) -> None:
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
