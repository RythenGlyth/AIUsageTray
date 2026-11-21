from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ai_usage_tray.core.storage import HistoryStorage
from ai_usage_tray.core.models import ProviderUsageSnapshot

SERIES_PALETTE = (
    QColor("#3b82f6"),  # Blue
    QColor("#10b981"),  # Emerald
    QColor("#f97316"),  # Orange
    QColor("#f43f5e"),  # Rose
    QColor("#a855f7"),  # Violet
    QColor("#eab308"),  # Amber
    QColor("#06b6d4"),  # Cyan
    QColor("#84cc16"),  # Lime
    QColor("#ec4899"),  # Pink
    QColor("#38bdf8"),  # Sky
    QColor("#818cf8"),  # Indigo
    QColor("#ef4444"),  # Red
    QColor("#2dd4bf"),  # Teal
    QColor("#facc15"),  # Gold
    QColor("#fb7185"),  # Coral
    QColor("#34d399"),  # Mint
)

DAYS_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(slots=True)
class BurnRateDetails:
    rate_pct_per_hour: float
    hours_to_empty: Optional[float]
    span_hours: float
    sample_count: int


def compute_burn_rate_details(points: list[tuple[datetime, float]]) -> Optional[BurnRateDetails]:
    """Compute burn rate (%/hr), hours to 0%, and sample span from recent non-reset segment."""
    if len(points) < 2:
        return None
    segment: list[tuple[datetime, float]] = [points[-1]]
    for i in range(len(points) - 2, -1, -1):
        if points[i + 1][1] - points[i][1] > 0.05:
            break
        segment.append(points[i])
    segment.reverse()
    if len(segment) < 2:
        return None

    dt_hours = (segment[-1][0] - segment[0][0]).total_seconds() / 3600.0
    if dt_hours <= 0.01:
        return None
    drop = segment[0][1] - segment[-1][1]
    if drop <= 0.0005:
        return BurnRateDetails(rate_pct_per_hour=0.0, hours_to_empty=None, span_hours=dt_hours, sample_count=len(segment))

    rate_fraction_per_hour = drop / dt_hours
    hours_to_empty = max(0.0, segment[-1][1] / rate_fraction_per_hour)
    return BurnRateDetails(
        rate_pct_per_hour=rate_fraction_per_hour * 100.0,
        hours_to_empty=hours_to_empty,
        span_hours=dt_hours,
        sample_count=len(segment),
    )


def compute_burn_rate_hours(points: list[tuple[datetime, float]]) -> Optional[float]:
    """Estimate hours until 0% remaining from recent downward consumption slope."""
    details = compute_burn_rate_details(points)
    return details.hours_to_empty if details is not None else None


def compute_average_consumption_rate(points: list[tuple[datetime, float]]) -> Optional[float]:
    """Compute average consumption rate (%/hr) across resets for a time window."""
    if len(points) < 2:
        return None
    dt_hours = (points[-1][0] - points[0][0]).total_seconds() / 3600.0
    if dt_hours <= 0.01:
        return None
    total_drop = 0.0
    for i in range(1, len(points)):
        diff = points[i - 1][1] - points[i][1]
        if diff > 0.0:
            total_drop += diff
    return (total_drop / dt_hours) * 100.0


def format_short_duration(hours: float) -> str:
    total_mins = max(0, int(round(hours * 60)))
    days, rem = divmod(total_mins, 1440)
    hrs, mins = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hrs}h"
    if hrs > 0:
        return f"{hrs}h {mins}m"
    return f"{mins}m"


def compute_reset_cycle_efficiency(points: list[tuple[datetime, float]]) -> tuple[int, Optional[float]]:
    """Detect completed reset cycles (upward jump > 5%) and return (cycle_count, avg_used_fraction)."""
    if len(points) < 2:
        return 0, None
    used_per_cycle: list[float] = []
    cycle_peak = points[0][1]
    for i in range(1, len(points)):
        prev_v = points[i - 1][1]
        cur_v = points[i][1]
        if cur_v - prev_v > 0.05:
            used = max(0.0, cycle_peak - prev_v)
            used_per_cycle.append(used)
            cycle_peak = cur_v
        else:
            cycle_peak = max(cycle_peak, cur_v)
    if not used_per_cycle:
        return 0, None
    return len(used_per_cycle), sum(used_per_cycle) / len(used_per_cycle)


def compute_hourly_heatmap(
    series_map: dict[tuple[str, str], list[tuple[datetime, float]]],
) -> list[list[float]]:
    """Compute a 7x24 matrix [weekday 0..6][hour 0..23] of total quota drop (0.0..N)."""
    grid = [[0.0 for _ in range(24)] for _ in range(7)]
    for pts in series_map.values():
        for i in range(1, len(pts)):
            _, v1 = pts[i - 1]
            t2, v2 = pts[i]
            drop = v1 - v2
            if 0.0 < drop <= 0.99:
                local_dt = t2.astimezone()
                grid[local_dt.weekday()][local_dt.hour] += drop
    return grid


class TimeSeriesChartWidget(QWidget):
    """Pure-QPainter multi-series quota chart with closest-line hover highlighting."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(230)
        self.setMouseTracking(True)
        self._series: list[tuple[str, QColor, list[tuple[datetime, float]]]] = []
        self._t_min: Optional[datetime] = None
        self._t_max: Optional[datetime] = None
        self.empty_message = "Collecting quota snapshots…"
        self._hover_x: Optional[float] = None
        self._hovered_idx: Optional[int] = None
        self._hovered_pt: Optional[tuple[datetime, float]] = None

    def set_series(
        self,
        series: list[tuple[str, QColor, list[tuple[datetime, float]]]],
        t_min: datetime,
        t_max: datetime,
    ) -> None:
        self._series = series
        self._t_min = t_min
        self._t_max = t_max
        self._hovered_idx = None
        self._hovered_pt = None
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(38.0, 10.0, max(20.0, self.width() - 50.0), max(20.0, self.height() - 32.0))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        rect = self._plot_rect()
        pos = event.position()
        if not rect.contains(pos) or not self._series or not self._t_min or not self._t_max:
            self._hover_x = None
            self._hovered_idx = None
            self._hovered_pt = None
            self.update()
            return
        self._hover_x = pos.x()

        span_s = max(1.0, (self._t_max - self._t_min).total_seconds())
        ratio = (pos.x() - rect.left()) / rect.width()
        target_ts = self._t_min.timestamp() + ratio * span_s

        best_idx: Optional[int] = None
        best_dist = float("inf")
        best_pt: Optional[tuple[datetime, float]] = None
        nearest_pts: list[tuple[int, str, tuple[datetime, float]]] = []

        for idx, (label, _, pts) in enumerate(self._series):
            if not pts:
                continue
            nearest = min(pts, key=lambda p: abs(p[0].timestamp() - target_ts))
            nearest_pts.append((idx, label, nearest))
            y_px = rect.bottom() - max(0.0, min(1.0, nearest[1])) * rect.height()
            dist = abs(y_px - pos.y())
            if dist < best_dist:
                best_dist = dist
                best_idx = idx
                best_pt = nearest

        self._hovered_idx = best_idx
        self._hovered_pt = best_pt
        self.update()

        ts_str = datetime.fromtimestamp(target_ts, tz=timezone.utc).astimezone().strftime("%b %d %H:%M")
        lines = [ts_str]
        if best_idx is not None:
            for idx, label, pt in nearest_pts:
                if idx == best_idx:
                    lines.insert(1, f"▶ {label}: {pt[1] * 100:.1f}% remaining")
                    break
        for idx, label, pt in nearest_pts:
            if idx != best_idx:
                lines.append(f"   {label}: {pt[1] * 100:.2f}%")
        QToolTip.showText(event.globalPosition().toPoint(), "\n".join(lines), self)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_x = None
        self._hovered_idx = None
        self._hovered_pt = None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._plot_rect()

        grid_pen = QPen(QColor(128, 128, 128, 45), 1.0)
        text_color = self.palette().text().color()
        small_font = QFont(self.font())
        small_font.setPointSize(9)
        painter.setFont(small_font)

        for pct in (0, 25, 50, 75, 100):
            y = rect.bottom() - (pct / 100.0) * rect.height()
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(
                QRectF(0, y - 7, rect.left() - 4, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{pct}%",
            )

        if not self._series or not self._t_min or not self._t_max:
            painter.setPen(text_color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.empty_message)
            return

        t0 = self._t_min.timestamp()
        span = max(1.0, self._t_max.timestamp() - t0)

        painter.setPen(QColor(160, 160, 160))
        tick_count = 4 if rect.width() >= 480 else 2
        for i in range(tick_count + 1):
            ratio = i / tick_count
            dt = self._t_min + (self._t_max - self._t_min) * ratio
            text = dt.astimezone().strftime("%b %d %H:%M" if span > 86400 else "%H:%M")
            x = rect.left() + ratio * rect.width()
            width = min(110, rect.width() / tick_count)
            left = max(rect.left(), min(x - width / 2, rect.right() - width))
            painter.drawText(QRectF(left, rect.bottom() + 4, width, 20),
                             Qt.AlignmentFlag.AlignCenter, text)

        # Draw non-hovered series first, then hovered series on top
        order = sorted(
            range(len(self._series)),
            key=lambda i: 1 if i == self._hovered_idx else 0,
        )
        for idx in order:
            _, color, pts = self._series[idx]
            if len(pts) < 2:
                continue
            path = QPainterPath()
            for i, (dt, val) in enumerate(pts):
                x = rect.left() + ((dt.timestamp() - t0) / span) * rect.width()
                y = rect.bottom() - max(0.0, min(1.0, val)) * rect.height()
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

            draw_color = QColor(color)
            if self._hovered_idx is not None and idx != self._hovered_idx:
                draw_color.setAlpha(75)
                width = 1.5
            elif idx == self._hovered_idx:
                width = 3.5
            else:
                width = 2.0

            painter.setPen(QPen(draw_color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)

        if self._hover_x is not None:
            painter.setPen(QPen(QColor(200, 200, 200, 130), 1.0, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(self._hover_x, rect.top()), QPointF(self._hover_x, rect.bottom()))

        if self._hovered_idx is not None and self._hovered_pt is not None:
            _, color, _ = self._series[self._hovered_idx]
            px = rect.left() + ((self._hovered_pt[0].timestamp() - t0) / span) * rect.width()
            py = rect.bottom() - max(0.0, min(1.0, self._hovered_pt[1])) * rect.height()
            painter.setBrush(color)
            painter.setPen(QPen(Qt.GlobalColor.white, 1.5))
            painter.drawEllipse(QPointF(px, py), 4.5, 4.5)


class UsageHeatmapWidget(QWidget):
    """7x24 Day-of-Week x Hour-of-Day quota burn heatmap."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(170)
        self.setMouseTracking(True)
        self._grid: list[list[float]] = [[0.0 for _ in range(24)] for _ in range(7)]
        self._max_val: float = 0.01

    def set_grid(self, grid: list[list[float]]) -> None:
        self._grid = grid
        flat = [v for row in grid for v in row]
        self._max_val = max(0.01, max(flat) if flat else 0.01)
        self.update()

    def _grid_rect(self) -> QRectF:
        return QRectF(32.0, 4.0, max(24.0, self.width() - 40.0), self.height() - 22.0)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        rect = self._grid_rect()
        pos = event.position()
        if not rect.contains(pos):
            return
        col = min(23, max(0, int((pos.x() - rect.left()) / (rect.width() / 24.0))))
        row = min(6, max(0, int((pos.y() - rect.top()) / (rect.height() / 7.0))))
        val = self._grid[row][col]
        QToolTip.showText(
            event.globalPosition().toPoint(),
            f"{DAYS_SHORT[row]} {col:02d}:00 — {val * 100:.2f}% quota used",
            self,
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._grid_rect()
        cw = rect.width() / 24.0
        ch = rect.height() / 7.0

        small_font = QFont(self.font())
        small_font.setPointSize(9)
        painter.setFont(small_font)

        for r in range(7):
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(
                QRectF(0, rect.top() + r * ch, rect.left() - 4, ch),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                DAYS_SHORT[r],
            )
            for c in range(24):
                val = self._grid[r][c]
                intensity = min(1.0, val / self._max_val) if val > 0 else 0.0
                if intensity <= 0.0:
                    color = QColor(128, 128, 128, 30)
                else:
                    alpha = int(60 + 195 * intensity)
                    color = QColor(46, 204, 113, alpha)
                cell = QRectF(rect.left() + c * cw + 1, rect.top() + r * ch + 1, max(1.0, cw - 2), max(1.0, ch - 2))
                painter.fillRect(cell, color)

        painter.setPen(QColor(160, 160, 160))
        for h in (0, 6, 12, 18, 23):
            x = rect.left() + h * cw
            painter.drawText(QRectF(x - 10, rect.bottom() + 2, 24, 14), Qt.AlignmentFlag.AlignCenter, f"{h:02d}")


def _clear_grid(grid: QGridLayout) -> None:
    while grid.count():
        item = grid.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.deleteLater()


def _label(text: str, bold: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    font = QFont(label.font())
    font.setBold(bold)
    label.setFont(font)
    return label


def format_burn_rate(rate: Optional[float]) -> str:
    if rate is None:
        return "Insufficient history"
    if rate < 0.05:
        return "<0.1 pp/h" if rate > 0 else "0.0 pp/h"
    return f"{rate:.1f} pp/h"


def forecast_status(details: Optional[BurnRateDetails], reset: Optional[datetime],
                    now: datetime) -> tuple[str, bool]:
    """Keep estimates within the known reset horizon; flag only actionable risk."""
    if reset is not None:
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
        reset_hours = (reset - now).total_seconds() / 3600
        if reset_hours <= 0:
            return "Awaiting reset", False
        if details is not None and details.hours_to_empty is not None:
            if details.hours_to_empty < reset_hours:
                return f"~{format_short_duration(details.hours_to_empty)} · May run out before reset", True
            return "Reset expected first", False
    if details is None:
        return "Insufficient history", False
    if details.hours_to_empty is None:
        return "Steady", False
    return f"~{format_short_duration(details.hours_to_empty)} · Reset unknown", False


class AnalyticsTabView(QWidget):
    """Provider-focused quota trends, actionable forecasts and optional usage patterns."""

    def __init__(self, storage: HistoryStorage, parent: Optional[QWidget] = None,
                 snapshots: Optional[dict[str, ProviderUsageSnapshot]] = None) -> None:
        super().__init__(parent)
        self.storage = storage
        self.snapshots = snapshots if snapshots is not None else {}
        self._disabled_keys: set[tuple[str, str]] = set()
        self._all_keys: list[tuple[str, str]] = []
        self._expanded_keys: set[tuple[str, str]] = set()
        self._compact = False
        self._refreshing = False
        self._legend_buttons: list[QPushButton] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(12)
        title = _label("Analytics", True)
        font = QFont(title.font())
        font.setPointSize(18)
        title.setFont(font)
        root.addWidget(title)

        self.controls = QGridLayout()
        self.controls.setSpacing(8)
        self.provider_combo = QComboBox()
        self.provider_combo.setMinimumWidth(0)
        self.provider_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.provider_combo.setMinimumContentsLength(12)
        self.provider_combo.setAccessibleName("Provider")
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self.range_combo = QComboBox()
        self.range_combo.setAccessibleName("Time range")
        for label, hours in (("6 Hours", 6), ("24 Hours", 24), ("7 Days", 168),
                             ("30 Days", 720), ("1 Year", 8760), ("Custom…", -1)):
            self.range_combo.addItem(label, hours)
        self.range_combo.setCurrentIndex(1)
        self.range_combo.currentIndexChanged.connect(self._on_range_changed)
        self.custom_controls = QWidget()
        custom_layout = QHBoxLayout(self.custom_controls)
        custom_layout.setContentsMargins(0, 0, 0, 0)
        self.custom_spin = QSpinBox()
        self.custom_spin.setRange(1, 8760)
        self.custom_spin.setValue(48)
        self.custom_spin.setAccessibleName("Custom range length")
        self.custom_spin.valueChanged.connect(self.refresh)
        custom_layout.addWidget(self.custom_spin)
        self.custom_unit = QComboBox()
        self.custom_unit.addItem("Hours", 1)
        self.custom_unit.addItem("Days", 24)
        self.custom_unit.currentIndexChanged.connect(self.refresh)
        custom_layout.addWidget(self.custom_unit)
        self.custom_controls.hide()
        self.controls.addWidget(self.provider_combo, 0, 0)
        self.controls.addWidget(self.range_combo, 0, 1)
        self.controls.addWidget(self.custom_controls, 1, 0, 1, 2)
        self.controls.setColumnStretch(0, 1)
        root.addLayout(self.controls)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setMinimumWidth(0)
        body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(0, 0, 6, 0)
        self.body_layout.setSpacing(12)

        chart_card = QFrame()
        chart_layout = QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        chart_bar = QHBoxLayout()
        chart_bar.addWidget(_label("Quota remaining", True), 1)
        self.choose_btn = QPushButton("Choose series")
        self.choose_btn.setCheckable(True)
        chart_bar.addWidget(self.choose_btn)
        chart_layout.addLayout(chart_bar)
        self.chart = TimeSeriesChartWidget()
        chart_layout.addWidget(self.chart)
        self.legend_grid = QGridLayout()
        self.legend_grid.setSpacing(6)
        chart_layout.addLayout(self.legend_grid)
        self.body_layout.addWidget(chart_card)

        self.selector = QWidget()
        selector_layout = QVBoxLayout(self.selector)
        selector_layout.setContentsMargins(0, 0, 0, 0)
        selector_bar = QHBoxLayout()
        selector_bar.addWidget(_label("Visible quotas", True), 1)
        for text, callback in (("All", self._select_all_stats), ("Clear", self._clear_all_stats)):
            btn = QPushButton(text)
            btn.clicked.connect(callback)
            selector_bar.addWidget(btn)
        selector_layout.addLayout(selector_bar)
        self.selector_grid = QGridLayout()
        selector_layout.addLayout(self.selector_grid)
        self.selector.hide()
        self.choose_btn.toggled.connect(self.selector.setVisible)
        self.body_layout.addWidget(self.selector)

        self.body_layout.addWidget(_label("Quota forecast", True))
        self.body_layout.addWidget(_label("Recent burn in percentage points/hour (pp/h). Estimates assume the recent pace continues."))
        self.forecast_grid = QGridLayout()
        self.forecast_grid.setSpacing(8)
        self.body_layout.addLayout(self.forecast_grid)

        self.patterns_btn = QPushButton("▸ Usage patterns")
        self.patterns_btn.setCheckable(True)
        self.patterns_btn.toggled.connect(self._toggle_patterns)
        self.body_layout.addWidget(self.patterns_btn)
        self.patterns = QWidget()
        patterns_layout = QVBoxLayout(self.patterns)
        patterns_layout.setContentsMargins(0, 0, 0, 0)
        patterns_layout.addWidget(_label("Activity by local day and hour", True))
        patterns_layout.addWidget(_label("Selected quotas only · Relative quota consumption · Less → More"))
        self.heatmap = UsageHeatmapWidget()
        patterns_layout.addWidget(self.heatmap)
        patterns_layout.addWidget(_label("Reset-cycle utilization", True))
        self.efficiency_label = _label("")
        patterns_layout.addWidget(self.efficiency_label)
        self.patterns.hide()
        self.body_layout.addWidget(self.patterns)

        self.token_section = QWidget()
        token_layout = QVBoxLayout(self.token_section)
        token_layout.setContentsMargins(0, 0, 0, 0)
        token_layout.addWidget(_label("Token usage", True))
        self.token_label = _label("")
        token_layout.addWidget(self.token_label)
        self.token_section.hide()
        self.body_layout.addWidget(self.token_section)
        self.body_layout.addStretch(1)
        self.scroll.setWidget(body)
        root.addWidget(self.scroll, 1)

    def _toggle_patterns(self, checked: bool) -> None:
        self.patterns.setVisible(checked)
        self.patterns_btn.setText(("▾" if checked else "▸") + " Usage patterns")

    def _provider_changed(self) -> None:
        self.refresh()

    def _on_range_changed(self) -> None:
        self.custom_controls.setVisible(self.range_combo.currentData() == -1)
        self.refresh()

    def _selected_hours(self) -> int:
        value = int(self.range_combo.currentData() or 24)
        if value > 0:
            return value
        return max(1, self.custom_spin.value() * int(self.custom_unit.currentData() or 1))

    def _provider_name(self, pid: str) -> str:
        snapshot = self.snapshots.get(pid)
        return snapshot.provider_name if snapshot else pid.replace("_", " ").title()

    def _bucket_metadata(self, key: tuple[str, str]):
        snapshot = self.snapshots.get(key[0])
        if snapshot:
            for group in snapshot.groups:
                for bucket in group.buckets:
                    if bucket.bucket_id == key[1]:
                        name = bucket.display_name.replace(" Limit Remaining", "").replace("5-Hour", "5-hour")
                        duplicates = sum(b.display_name == bucket.display_name for b in snapshot.all_buckets)
                        if duplicates > 1:
                            name = f"{group.display_name} · {name}"
                        # Failed polls retain history, but their reset metadata is not reliable.
                        return name, bucket.reset_time if not snapshot.error else None
        return key[1].replace("_", " ").replace("-", " ").title(), None

    def _series_name(self, key: tuple[str, str]) -> str:
        return f"{self._provider_name(key[0])} · {self._bucket_metadata(key)[0]}"

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = self.width() < 680
        if compact != self._compact:
            self._compact = compact
            if self._all_keys:
                self.refresh()

    def _toggle_details(self, key: tuple[str, str]) -> None:
        if key in self._expanded_keys:
            self._expanded_keys.remove(key)
        else:
            self._expanded_keys.add(key)
        self.refresh()

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self._refresh()
        finally:
            self._refreshing = False

    def _refresh(self) -> None:
        now = datetime.now(timezone.utc)
        hours = self._selected_hours()
        since = now - timedelta(hours=hours)
        series_map = self.storage.get_all_series_since(since)
        providers = sorted(set(pid for pid, _ in series_map) | set(self.snapshots))
        previous = self.provider_combo.currentData()
        choices = [(self._provider_name(pid), pid) for pid in providers]
        if providers:
            choices.append(("Compare providers", "*"))
        if [self.provider_combo.itemData(i) for i in range(self.provider_combo.count())] != [p for _, p in choices]:
            self.provider_combo.blockSignals(True)
            self.provider_combo.clear()
            for label, pid in choices:
                self.provider_combo.addItem(label, pid)
            idx = self.provider_combo.findData(previous)
            self.provider_combo.setCurrentIndex(max(0, idx))
            self.provider_combo.blockSignals(False)
        provider = self.provider_combo.currentData()
        items = [(key, pts) for key, pts in sorted(series_map.items()) if provider == "*" or key[0] == provider]
        self._all_keys = [key for key, _ in items]
        for grid in (self.legend_grid, self.selector_grid, self.forecast_grid):
            _clear_grid(grid)
        self._legend_buttons = []
        visible = []
        colors = {key: SERIES_PALETTE[idx % len(SERIES_PALETTE)] for idx, key in enumerate(sorted(series_map))}
        columns = 1 if self._compact else 2
        for idx, (key, pts) in enumerate(items):
            label = self._series_name(key)
            color = colors[key]
            checked = key not in self._disabled_keys
            legend = QPushButton()
            legend.setCheckable(True)
            legend.setChecked(checked)
            legend.setMinimumWidth(0)
            legend.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            legend.setAccessibleName(label)
            legend_layout = QVBoxLayout(legend)
            legend_layout.setContentsMargins(8, 5, 8, 5)
            legend_text = _label(label if provider == "*" else self._bucket_metadata(key)[0])
            legend_text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            if not checked:
                legend_text.setStyleSheet("color: #888;")
            legend_layout.addWidget(legend_text)
            legend.setToolTip(label + " · Click to toggle")
            legend.setStyleSheet(f"QPushButton {{ text-align: left; border: 1px solid {color.name()}; border-radius: 5px; }} QPushButton:!checked {{ color: #888; border-color: #555; }}")
            legend.clicked.connect(lambda _c=False, k=key: self._toggle_key_flip(k))
            self.legend_grid.addWidget(legend, idx // columns, idx % columns)
            self._legend_buttons.append(legend)
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            cb = QCheckBox()
            cb.setChecked(checked)
            cb.setAccessibleName(label)
            cb.toggled.connect(lambda value, k=key: self._toggle_series(k, value))
            row_layout.addWidget(cb)
            row_layout.addWidget(_label(label), 1)
            solo = QPushButton("Only")
            solo.setToolTip("Show only " + label)
            solo.clicked.connect(lambda _c=False, k=key: self._solo_key(k))
            row_layout.addWidget(solo)
            self.selector_grid.addWidget(row, idx, 0)
            if checked:
                visible.append((key, pts, label, color))
        self.chart.empty_message = "Choose a quota to show its history." if items else "Collecting quota snapshots…"
        self.chart.set_series([(label, color, pts) for _, pts, label, color in visible], since, now)
        self.choose_btn.setText(f"Choose series ({len(visible)}/{len(items)})")

        forecasts = []
        for key, pts, label, color in visible:
            name, reset = self._bucket_metadata(key)
            details = compute_burn_rate_details(pts)
            status, risky = forecast_status(details, reset, now)
            forecasts.append((not risky, details.hours_to_empty if details and details.hours_to_empty is not None else float("inf"), key, pts, label, color, reset, details, status))
        forecasts.sort(key=lambda row: (row[0], row[1], row[2]))
        year_map = self.storage.get_all_series_since(now - timedelta(days=365)) if self._expanded_keys else {}
        for idx, (_, _, key, pts, label, color, reset, details, status) in enumerate(forecasts):
            frame = QFrame()
            frame.setObjectName("ForecastRow")
            frame.setMinimumWidth(0)
            frame.setStyleSheet("QFrame#ForecastRow { background: rgba(128,128,128,0.06); border: 1px solid rgba(128,128,128,0.16); border-radius: 8px; }")
            layout = QGridLayout(frame)
            layout.setContentsMargins(12, 10, 12, 10)
            layout.setHorizontalSpacing(12)
            name_label = _label(label if provider == "*" else self._bucket_metadata(key)[0], True)
            name_label.setToolTip(label)
            btn = QPushButton("▾" if key in self._expanded_keys else "▸")
            btn.setFixedWidth(28)
            btn.setAccessibleName("Historical rates for " + label)
            btn.setToolTip("Show historical burn rates")
            btn.clicked.connect(lambda _c=False, k=key: self._toggle_details(k))
            rate = format_burn_rate(details.rate_pct_per_hour if details else None)
            reset_hours = None
            if reset:
                reset = reset.replace(tzinfo=timezone.utc) if reset.tzinfo is None else reset
                reset_hours = (reset - now).total_seconds() / 3600
            reset_text = (format_short_duration(reset_hours) if reset_hours is not None and reset_hours > 0
                          else "Awaiting reset" if reset_hours is not None else "Unknown")
            status_label = _label(status)
            if status.endswith("May run out before reset"):
                status_label.setStyleSheet("color: #f59e0b;")
            if self._compact:
                layout.addWidget(name_label, 0, 0, 1, 3)
                layout.addWidget(btn, 0, 3)
                metrics = [("Remaining", f"{pts[-1][1] * 100:.1f}%"), ("Recent burn", rate), ("Resets in", reset_text)]
                for row, (heading, value) in enumerate(metrics, 1):
                    layout.addWidget(_label(heading), row, 0, 1, 2)
                    layout.addWidget(_label(value), row, 2, 1, 2)
                layout.addWidget(_label("Estimated empty"), 4, 0, 1, 4)
                layout.addWidget(status_label, 5, 0, 1, 4)
                for col in range(4):
                    layout.setColumnStretch(col, 1)
                detail_row, span = 6, 4
            else:
                cells = [("Quota", name_label), ("Remaining", _label(f"{pts[-1][1] * 100:.1f}%")),
                         ("Recent burn", _label(rate)), ("Estimated empty", status_label), ("Resets in", _label(reset_text))]
                for col, (heading, value) in enumerate(cells):
                    layout.addWidget(_label(heading), 0, col)
                    layout.addWidget(value, 1, col)
                    layout.setColumnStretch(col, (3, 2, 2, 4, 2)[col])
                layout.addWidget(btn, 0, 5, 2, 1)
                detail_row, span = 2, 6
            if key in self._expanded_keys:
                windows = [("1 hour", 1), ("1 day", 24), ("1 week", 168), ("30 days", 720), ("1 year", 8760)]
                if hours not in [h for _, h in windows]:
                    windows.append(("Selected range", hours))
                texts = []
                for title, window in windows:
                    sub = [pt for pt in year_map.get(key, pts) if pt[0] >= now - timedelta(hours=window)]
                    texts.append(f"{title}: {format_burn_rate(compute_average_consumption_rate(sub))}")
                layout.addWidget(_label("Average consumption · Available samples in each window\n" + " · ".join(texts)), detail_row, 0, 1, span)
                if details:
                    layout.addWidget(_label(f"Recent pace: {details.sample_count} samples over {format_short_duration(details.span_hours)}, since the last observed reset within this range."), detail_row + 1, 0, 1, span)
            self.forecast_grid.addWidget(frame, idx, 0)
        if not forecasts:
            self.forecast_grid.addWidget(_label("Select a quota to see forecasts." if items else "Collecting quota snapshots for this provider…"), 0, 0)
        selected_map = {key: pts for key, pts, _, _ in visible}
        self.heatmap.set_grid(compute_hourly_heatmap(selected_map))
        efficiency = []
        for key, pts, label, _ in visible:
            cycles, used = compute_reset_cycle_efficiency(pts)
            if cycles and used is not None:
                efficiency.append(f"{label}: {used * 100:.1f}% average used · {cycles} completed cycles")
        self.efficiency_label.setText("\n".join(efficiency) or "No completed reset cycles in the selected range.")
        tokens = [row for row in self.storage.get_token_usage_summary(since) if provider == "*" or row[0] == provider]
        self.token_section.setVisible(bool(tokens))
        self.token_label.setText("\n".join(f"{self._provider_name(pid)} · {model}: {inp:,} input · {out:,} output · {cached:,} cached" for pid, model, inp, out, cached in tokens))

    def focus_series(self, provider_id: str, bucket_id: str) -> None:
        self.refresh()
        index = self.provider_combo.findData(provider_id)
        if index >= 0:
            self.provider_combo.blockSignals(True)
            self.provider_combo.setCurrentIndex(index)
            self.provider_combo.blockSignals(False)
        target = (provider_id, bucket_id)
        all_series = self.storage.get_all_series_since(datetime.now(timezone.utc) - timedelta(days=365))
        self._disabled_keys = {key for key in all_series if key[0] == provider_id and key != target}
        self.refresh()

    def _select_all_stats(self) -> None:
        self._disabled_keys.difference_update(self._all_keys)
        self.refresh()

    def _clear_all_stats(self) -> None:
        self._disabled_keys.update(self._all_keys)
        self.refresh()

    def _solo_key(self, key: tuple[str, str]) -> None:
        self._disabled_keys.update(k for k in self._all_keys if k != key)
        self._disabled_keys.discard(key)
        self.refresh()

    def _toggle_key_flip(self, key: tuple[str, str]) -> None:
        self._toggle_series(key, key in self._disabled_keys)

    def _toggle_series(self, key: tuple[str, str], checked: bool) -> None:
        if checked:
            self._disabled_keys.discard(key)
        else:
            self._disabled_keys.add(key)
        self.refresh()
