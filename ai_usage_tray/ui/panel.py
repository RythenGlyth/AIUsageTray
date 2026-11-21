from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from PyQt6.QtCore import QEvent, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QKeyEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ai_usage_tray.core.config import ConfigManager
from ai_usage_tray.core.models import ProviderUsageSnapshot, format_countdown, format_remaining_percent
from ai_usage_tray.core.provider import ProviderRegistry
from ai_usage_tray.core.storage import HistoryStorage
from ai_usage_tray.ui.analytics import AnalyticsTabView
from ai_usage_tray.ui.widgets import QuotaBucketCard, RingGaugeWidget


class ProviderTabView(QWidget):
    """Scrollable detail view for a single AI provider."""

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
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        summary_frame = QFrame()
        summary_frame.setObjectName("ProviderSummary")
        summary_frame.setStyleSheet(
            """
            QFrame#ProviderSummary {
                background-color: rgba(128, 128, 128, 0.08);
                border: 1px solid rgba(128, 128, 128, 0.14);
                border-radius: 10px;
            }
            """
        )
        summary_layout = QHBoxLayout(summary_frame)
        summary_layout.setContentsMargins(14, 10, 14, 10)
        summary_layout.setSpacing(12)

        self.ring = RingGaugeWidget(size=54)
        summary_layout.addWidget(self.ring)

        meta_col = QVBoxLayout()
        meta_col.setSpacing(3)
        self.title_label = QLabel(provider_id)
        t_font = QFont(self.title_label.font())
        t_font.setBold(True)
        t_font.setPointSize(11)
        self.title_label.setFont(t_font)
        meta_col.addWidget(self.title_label)

        self.source_label = QLabel("Waiting for data...")
        src_font = QFont(self.source_label.font())
        src_font.setPointSize(8)
        self.source_label.setFont(src_font)
        self.source_label.setStyleSheet("color: rgba(170, 170, 170, 0.95);")
        meta_col.addWidget(self.source_label)

        self.badges_label = QLabel("")
        badge_font = QFont(self.badges_label.font())
        badge_font.setPointSize(8)
        self.badges_label.setFont(badge_font)
        self.badges_label.setStyleSheet("color: rgba(150, 150, 150, 0.90);")
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

    def set_disabled_state(self, disabled: bool, display_name: str = "") -> None:
        self._disabled = disabled
        if display_name:
            self.title_label.setText(display_name)
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
        self.title_label.setText(snap.provider_name)
        if self._disabled:
            return
        self._clear_body()

        meta_parts = [snap.source_label]
        if snap.plan_name:
            meta_parts.append(snap.plan_name)
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
                "color: #e74c3c; background-color: rgba(231, 76, 60, 0.10); padding: 10px; border-radius: 8px;"
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


class OverviewView(QWidget):
    """2-column summary grid showing the tightest limit per enabled provider."""

    def __init__(self, on_select_provider: Callable[[str], None], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.on_select_provider = on_select_provider
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        title = QLabel("All Providers Overview")
        t_font = QFont(title.font())
        t_font.setBold(True)
        t_font.setPointSize(11)
        title.setFont(t_font)
        root.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        self.grid = QGridLayout(body)
        self.grid.setContentsMargins(0, 0, 4, 0)
        self.grid.setSpacing(10)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    def refresh(
        self,
        ordered_providers: list,
        snapshots: dict[str, ProviderUsageSnapshot],
        config_manager: ConfigManager,
    ) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        col = 0
        row = 0
        for provider in ordered_providers:
            pid = provider.provider_id
            if not config_manager.config.get_provider(pid).enabled:
                continue
            snap = snapshots.get(pid)

            card = QPushButton()
            card.setObjectName("OverviewCard")
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.setMinimumHeight(88)
            card.setStyleSheet(
                """
                QPushButton#OverviewCard {
                    text-align: left;
                    background-color: rgba(128, 128, 128, 0.06);
                    border: 1px solid rgba(128, 128, 128, 0.16);
                    border-radius: 10px;
                    padding: 10px;
                }
                QPushButton#OverviewCard:hover {
                    background-color: rgba(128, 128, 128, 0.13);
                    border: 1px solid rgba(128, 128, 128, 0.30);
                }
                """
            )
            card.clicked.connect(lambda _=False, p_id=pid: self.on_select_provider(p_id))

            hl = QHBoxLayout(card)
            hl.setContentsMargins(10, 8, 10, 8)
            hl.setSpacing(10)

            ring = RingGaugeWidget(size=46)
            if snap and snap.lowest_remaining_fraction is not None:
                low = snap.lowest_remaining_fraction
                ring.set_value(float(low), f"{int(round(float(low) * 100))}%")
            else:
                ring.set_value(None, "—")
            hl.addWidget(ring)

            vl = QVBoxLayout()
            vl.setSpacing(2)
            name_lbl = QLabel(provider.display_name)
            nf = QFont(name_lbl.font())
            nf.setBold(True)
            nf.setPointSize(9)
            name_lbl.setFont(nf)
            vl.addWidget(name_lbl)

            if snap is None:
                sub = "Loading..."
            elif snap.error:
                sub = "Error / Auth needed"
            elif snap.all_buckets:
                tightest = min(snap.all_buckets, key=lambda b: b.remaining_fraction)
                sub = f"{tightest.remaining_percent_str}\n{format_countdown(tightest.reset_time)}"
            else:
                sub = "No active windows"
            sub_lbl = QLabel(sub)
            sf = QFont(sub_lbl.font())
            sf.setPointSize(8)
            sub_lbl.setFont(sf)
            sub_lbl.setStyleSheet("color: rgba(165, 165, 165, 0.95);")
            vl.addWidget(sub_lbl)
            hl.addLayout(vl, 1)

            self.grid.addWidget(card, row, col)
            col += 1
            if col >= 2:
                col = 0
                row += 1

        self.grid.setRowStretch(row + 1, 1)


class UsagePopupPanel(QWidget):
    """Modern two-pane KDE Plasma window with reorderable provider rail, Overview, and Analytics."""

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
        self._snapshots: dict[str, ProviderUsageSnapshot] = {}

        self.setWindowTitle("AI Usage Limits")
        self.setMinimumSize(620, 480)
        self.resize(700, 560)

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        # Left Navigation Rail
        rail = QFrame()
        rail.setObjectName("NavRail")
        rail.setFixedWidth(190)
        rail.setStyleSheet(
            """
            QFrame#NavRail {
                background-color: rgba(128, 128, 128, 0.06);
                border: 1px solid rgba(128, 128, 128, 0.14);
                border-radius: 10px;
            }
            """
        )
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(8, 8, 8, 8)
        rail_layout.setSpacing(6)

        self.overview_btn = QPushButton("Overview")
        self.overview_btn.setIcon(QIcon.fromTheme("view-grid"))
        self.overview_btn.setCheckable(True)
        self.overview_btn.setChecked(True)
        self.overview_btn.clicked.connect(self._show_overview)
        rail_layout.addWidget(self.overview_btn)

        self.provider_list = QListWidget()
        self.provider_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.provider_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.provider_list.setFrameShape(QFrame.Shape.NoFrame)
        self.provider_list.setStyleSheet(
            """
            QListWidget {
                background: transparent;
                outline: none;
            }
            QListWidget::item {
                padding: 7px 8px;
                border-radius: 6px;
                margin: 1px 0px;
            }
            QListWidget::item:selected {
                background-color: palette(highlight);
                color: palette(highlighted-text);
            }
            """
        )
        self.provider_list.currentItemChanged.connect(self._on_provider_selected)
        self.provider_list.model().rowsMoved.connect(self._on_rows_moved)
        rail_layout.addWidget(self.provider_list, 1)

        self.analytics_btn = QPushButton("Analytics")
        self.analytics_btn.setIcon(QIcon.fromTheme("view-statistics"))
        self.analytics_btn.setCheckable(True)
        self.analytics_btn.clicked.connect(self._show_analytics)
        rail_layout.addWidget(self.analytics_btn)

        self.trend_combo = QComboBox()
        self.trend_combo.setToolTip("Sparkline & forecast lookback window")
        for label, hrs in (("1h", 1), ("1d", 24), ("7d", 168), ("30d", 720)):
            self.trend_combo.addItem(f"Trend: {label}", hrs)
        idx = self.trend_combo.findData(self.config_manager.config.trend_window_hours)
        self.trend_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self.trend_combo.currentIndexChanged.connect(self._on_trend_changed)
        rail_layout.addWidget(self.trend_combo)

        action_row = QHBoxLayout()
        action_row.setSpacing(6)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setIcon(QIcon.fromTheme("view-refresh"))
        refresh_btn.setIconSize(QSize(16, 16))
        refresh_btn.clicked.connect(self.on_refresh_all)
        action_row.addWidget(refresh_btn, 1)

        settings_btn = QPushButton()
        settings_btn.setIcon(QIcon.fromTheme("configure"))
        settings_btn.setIconSize(QSize(16, 16))
        settings_btn.setToolTip("Settings & Accounts")
        settings_btn.setFixedWidth(32)
        settings_btn.clicked.connect(self.on_open_settings)
        action_row.addWidget(settings_btn)
        rail_layout.addLayout(action_row)

        self.status_label = QLabel("")
        s_font = QFont(self.status_label.font())
        s_font.setPointSize(7)
        self.status_label.setFont(s_font)
        self.status_label.setStyleSheet("color: rgba(150, 150, 150, 0.85);")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rail_layout.addWidget(self.status_label)

        root.addWidget(rail)

        # Right Stacked Content
        self.stack = QStackedWidget()
        self.overview_view = OverviewView(on_select_provider=self.select_provider)
        self.stack.addWidget(self.overview_view)

        self.provider_views: dict[str, ProviderTabView] = {}
        self.provider_items: dict[str, QListWidgetItem] = {}

        for provider in self._ordered_providers():
            pid = provider.provider_id
            item = QListWidgetItem(provider.display_name)
            item.setData(Qt.ItemDataRole.UserRole, pid)
            self.provider_list.addItem(item)
            self.provider_items[pid] = item

            view = ProviderTabView(
                provider_id=pid,
                storage=self.storage,
                get_trend_hours=self._current_trend_hours,
                on_connect_oauth=self.on_connect_oauth,
            )
            self.provider_views[pid] = view
            self.stack.addWidget(view)

        self.analytics_view = AnalyticsTabView(storage=self.storage)
        self.stack.addWidget(self.analytics_view)

        root.addWidget(self.stack, 1)

        self.refresh_provider_states()
        self._show_overview()

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            if isinstance(obj, QWidget) and (obj is self or self.isAncestorOf(obj)):
                mods = event.modifiers()
                key = event.key()
                if mods & Qt.KeyboardModifier.ControlModifier:
                    if key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and (mods & Qt.KeyboardModifier.ShiftModifier)):
                        if event.type() == QEvent.Type.ShortcutOverride:
                            event.accept()
                            return True
                        self._cycle_view(-1)
                        return True
                    if key == Qt.Key.Key_Tab:
                        if event.type() == QEvent.Type.ShortcutOverride:
                            event.accept()
                            return True
                        self._cycle_view(1)
                        return True
        return super().eventFilter(obj, event)

    def _cycle_view(self, step: int) -> None:
        # Order: Overview (-1), provider rows (0..N-1), Analytics (N)
        count = self.provider_list.count()
        cur_widget = self.stack.currentWidget()
        if cur_widget is self.overview_view:
            cur_idx = -1
        elif cur_widget is self.analytics_view:
            cur_idx = count
        else:
            cur_idx = self.provider_list.currentRow()

        total = count + 2  # overview + providers + analytics
        # Map [-1 .. count] -> [0 .. total-1]
        pos = (cur_idx + 1 + step) % total
        target_idx = pos - 1
        if target_idx == -1:
            self._show_overview()
        elif target_idx == count:
            self._show_analytics()
        else:
            self.provider_list.setCurrentRow(target_idx)

    def _ordered_providers(self):
        all_providers = {p.provider_id: p for p in self.registry.all()}
        order = self.config_manager.config.tab_order
        ordered = [all_providers[pid] for pid in order if pid in all_providers]
        for p in self.registry.all():
            if p not in ordered:
                ordered.append(p)
        return ordered

    def _on_rows_moved(self, *_args) -> None:
        new_order: list[str] = []
        for i in range(self.provider_list.count()):
            item = self.provider_list.item(i)
            if item is not None:
                pid = item.data(Qt.ItemDataRole.UserRole)
                if pid:
                    new_order.append(str(pid))
        self.config_manager.config.tab_order = new_order
        self.config_manager.save()
        self.overview_view.refresh(self._ordered_providers(), self._snapshots, self.config_manager)

    def _update_item_label(self, pid: str) -> None:
        item = self.provider_items.get(pid)
        provider = self.registry.get(pid)
        if item is None or provider is None:
            return
        enabled = self.config_manager.config.get_provider(pid).enabled
        if not enabled:
            item.setText(f"{provider.display_name}  (Off)")
            item.setForeground(QColor(130, 130, 130, 130))
            return

        item.setForeground(self.palette().text().color())
        snap = self._snapshots.get(pid)
        if snap is not None and snap.lowest_remaining_fraction is not None:
            pct = int(round(float(snap.lowest_remaining_fraction) * 100))
            item.setText(f"{provider.display_name}  •  {pct}%")
        elif snap is not None and snap.error:
            item.setText(f"{provider.display_name}  •  !")
        else:
            item.setText(provider.display_name)

    def refresh_provider_states(self) -> None:
        for pid, view in self.provider_views.items():
            provider = self.registry.get(pid)
            enabled = self.config_manager.config.get_provider(pid).enabled
            view.set_disabled_state(not enabled, provider.display_name if provider else pid)
            self._update_item_label(pid)
        self.overview_view.refresh(self._ordered_providers(), self._snapshots, self.config_manager)

    def select_provider(self, provider_id: str) -> None:
        item = self.provider_items.get(provider_id)
        if item is not None:
            self.provider_list.setCurrentItem(item)

    def _on_provider_selected(self, current: Optional[QListWidgetItem], _prev: Optional[QListWidgetItem]) -> None:
        if current is None:
            return
        pid = str(current.data(Qt.ItemDataRole.UserRole))
        view = self.provider_views.get(pid)
        if view is not None:
            self.overview_btn.setChecked(False)
            self.analytics_btn.setChecked(False)
            self.stack.setCurrentWidget(view)

    def _show_overview(self) -> None:
        self.provider_list.blockSignals(True)
        self.provider_list.setCurrentRow(-1)
        self.provider_list.blockSignals(False)
        self.overview_btn.setChecked(True)
        self.analytics_btn.setChecked(False)
        self.overview_view.refresh(self._ordered_providers(), self._snapshots, self.config_manager)
        self.stack.setCurrentWidget(self.overview_view)

    def _show_analytics(self) -> None:
        self.provider_list.blockSignals(True)
        self.provider_list.setCurrentRow(-1)
        self.provider_list.blockSignals(False)
        self.overview_btn.setChecked(False)
        self.analytics_btn.setChecked(True)
        self.analytics_view.refresh()
        self.stack.setCurrentWidget(self.analytics_view)

    def _current_trend_hours(self) -> int:
        return int(self.trend_combo.currentData() or 24)

    def _on_trend_changed(self) -> None:
        hrs = self._current_trend_hours()
        self.config_manager.config.trend_window_hours = hrs
        self.config_manager.save()
        for view in self.provider_views.values():
            view.rerender_trends()

    def update_snapshot(self, snapshot: ProviderUsageSnapshot) -> None:
        self._snapshots[snapshot.provider_id] = snapshot
        view = self.provider_views.get(snapshot.provider_id)
        if view is not None:
            view.update_snapshot(snapshot)
        self._update_item_label(snapshot.provider_id)
        if self.stack.currentWidget() is self.overview_view:
            self.overview_view.refresh(self._ordered_providers(), self._snapshots, self.config_manager)
        elif self.stack.currentWidget() is self.analytics_view:
            self.analytics_view.refresh()
        self.status_label.setText(datetime.now(timezone.utc).astimezone().strftime("Sync %H:%M:%S"))

    def set_refreshing(self, provider_id: str) -> None:
        self.status_label.setText(f"Refreshing {provider_id}...")

    def show_window(self) -> None:
        if self.stack.currentWidget() is self.analytics_view:
            self.analytics_view.refresh()
        elif self.stack.currentWidget() is self.overview_view:
            self.overview_view.refresh(self._ordered_providers(), self._snapshots, self.config_manager)
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
