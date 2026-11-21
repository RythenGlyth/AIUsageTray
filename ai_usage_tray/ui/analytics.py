from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ai_usage_tray.core.storage import HistoryStorage

SERIES_PALETTE = (
    QColor("#3498db"),
    QColor("#2ecc71"),
    QColor("#e67e22"),
    QColor("#9b59b6"),
    QColor("#1abc9c"),
    QColor("#e74c3c"),
    QColor("#f1c40f"),
    QColor("#fd79a8"),
)

DAYS_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def compute_burn_rate_hours(points: list[tuple[datetime, float]]) -> Optional[float]:
    """Estimate hours until 0% remaining from recent downward consumption slope.

    Returns None if there is no net downward consumption.
    """
    if len(points) < 2:
        return None
    # Walk backwards until we hit a reset jump (> +5%)
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
        return None
    rate_per_hour = drop / dt_hours
    return max(0.0, segment[-1][1] / rate_per_hour)


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
            t1, v1 = pts[i - 1]
            t2, v2 = pts[i]
            drop = v1 - v2
            if 0.0 < drop <= 0.99:
                local_dt = t2.astimezone()
                grid[local_dt.weekday()][local_dt.hour] += drop
    return grid


class TimeSeriesChartWidget(QWidget):
    """Pure-QPainter multi-series quota chart with hover crosshair."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self._series: list[tuple[str, QColor, list[tuple[datetime, float]]]] = []
        self._t_min: Optional[datetime] = None
        self._t_max: Optional[datetime] = None
        self._hover_x: Optional[float] = None

    def set_series(
        self,
        series: list[tuple[str, QColor, list[tuple[datetime, float]]]],
        t_min: datetime,
        t_max: datetime,
    ) -> None:
        self._series = series
        self._t_min = t_min
        self._t_max = t_max
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(38.0, 10.0, max(20.0, self.width() - 50.0), max(20.0, self.height() - 32.0))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        rect = self._plot_rect()
        pos = event.position()
        if not rect.contains(pos) or not self._series or not self._t_min or not self._t_max:
            self._hover_x = None
            self.update()
            return
        self._hover_x = pos.x()
        self.update()

        span_s = max(1.0, (self._t_max - self._t_min).total_seconds())
        ratio = (pos.x() - rect.left()) / rect.width()
        target_ts = self._t_min.timestamp() + ratio * span_s

        lines = [datetime.fromtimestamp(target_ts, tz=timezone.utc).astimezone().strftime("%b %d %H:%M")]
        for label, _, pts in self._series:
            if not pts:
                continue
            nearest = min(pts, key=lambda p: abs(p[0].timestamp() - target_ts))
            lines.append(f"{label}: {nearest[1] * 100:.2f}%")
        QToolTip.showText(event.globalPosition().toPoint(), "\n".join(lines), self)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_x = None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._plot_rect()

        # Grid lines
        grid_pen = QPen(QColor(128, 128, 128, 45), 1.0)
        text_color = self.palette().text().color()
        small_font = QFont(self.font())
        small_font.setPointSize(7)
        painter.setFont(small_font)

        for pct in (0, 25, 50, 75, 100):
            y = rect.bottom() - (pct / 100.0) * rect.height()
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(QRectF(0, y - 7, rect.left() - 4, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{pct}%")

        if not self._series or not self._t_min or not self._t_max:
            painter.setPen(text_color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Collecting quota snapshots...")
            return

        t0 = self._t_min.timestamp()
        span = max(1.0, self._t_max.timestamp() - t0)

        # X-axis labels
        painter.setPen(QColor(160, 160, 160))
        painter.drawText(
            QRectF(rect.left(), rect.bottom() + 4, 90, 16),
            Qt.AlignmentFlag.AlignLeft,
            self._t_min.astimezone().strftime("%b %d %H:%M"),
        )
        painter.drawText(
            QRectF(rect.right() - 90, rect.bottom() + 4, 90, 16),
            Qt.AlignmentFlag.AlignRight,
            self._t_max.astimezone().strftime("%b %d %H:%M"),
        )

        # Series lines
        for _, color, pts in self._series:
            if len(pts) < 2:
                continue
            path = QPainterPath()
            started = False
            for dt, val in pts:
                x = rect.left() + ((dt.timestamp() - t0) / span) * rect.width()
                y = rect.bottom() - max(0.0, min(1.0, val)) * rect.height()
                if not started:
                    path.moveTo(x, y)
                    started = True
                else:
                    path.lineTo(x, y)
            painter.setPen(QPen(color, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)

        if self._hover_x is not None:
            painter.setPen(QPen(QColor(200, 200, 200, 140), 1.0, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(self._hover_x, rect.top()), QPointF(self._hover_x, rect.bottom()))


class UsageHeatmapWidget(QWidget):
    """7x24 Day-of-Week x Hour-of-Day quota burn heatmap."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(124)
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
        small_font.setPointSize(7)
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


class AnalyticsTabView(QWidget):
    """Analytics dashboard tab with time-series chart, burn-rate forecast, heatmap, and cycle efficiency."""

    def __init__(self, storage: HistoryStorage, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.storage = storage
        self._disabled_keys: set[tuple[str, str]] = set()

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 8, 6, 6)
        root.setSpacing(8)

        # Controls bar
        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("Range:"))
        self.range_combo = QComboBox()
        for label, hours in (("6 Hours", 6), ("24 Hours", 24), ("7 Days", 168), ("30 Days", 720)):
            self.range_combo.addItem(label, hours)
        self.range_combo.setCurrentIndex(1)
        self.range_combo.currentIndexChanged.connect(self.refresh)
        ctrl_row.addWidget(self.range_combo)
        ctrl_row.addStretch(1)
        root.addLayout(ctrl_row)

        # Scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(0, 0, 4, 0)
        self.body_layout.setSpacing(10)

        self.chart = TimeSeriesChartWidget()
        self.body_layout.addWidget(self.chart)

        self.legend_layout = QVBoxLayout()
        self.legend_layout.setSpacing(2)
        self.body_layout.addLayout(self.legend_layout)

        self.forecast_title = QLabel("Burn-Rate & Time-to-Empty Forecast")
        f_font = QFont(self.forecast_title.font())
        f_font.setBold(True)
        self.forecast_title.setFont(f_font)
        self.body_layout.addWidget(self.forecast_title)

        self.forecast_label = QLabel("Collecting data...")
        self.forecast_label.setWordWrap(True)
        self.forecast_label.setStyleSheet("color: rgba(180, 180, 180, 0.95);")
        self.body_layout.addWidget(self.forecast_label)

        heat_title = QLabel("Activity Heatmap (Local Time)")
        heat_title.setFont(f_font)
        self.body_layout.addWidget(heat_title)

        self.heatmap = UsageHeatmapWidget()
        self.body_layout.addWidget(self.heatmap)

        eff_title = QLabel("Reset-Cycle Efficiency")
        eff_title.setFont(f_font)
        self.body_layout.addWidget(eff_title)

        self.eff_label = QLabel("No completed reset cycles recorded yet.")
        self.eff_label.setWordWrap(True)
        self.eff_label.setStyleSheet("color: rgba(180, 180, 180, 0.95);")
        self.body_layout.addWidget(self.eff_label)

        tok_title = QLabel("Token & Cost Breakdown")
        tok_title.setFont(f_font)
        self.body_layout.addWidget(tok_title)

        self.token_label = QLabel("No token events recorded yet (ready for local CLI log ingestion).")
        self.token_label.setWordWrap(True)
        self.token_label.setStyleSheet("color: rgba(160, 160, 160, 0.90);")
        self.body_layout.addWidget(self.token_label)

        self.body_layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    def _clear_legend(self) -> None:
        while self.legend_layout.count():
            item = self.legend_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def refresh(self) -> None:
        hours = int(self.range_combo.currentData() or 24)
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=hours)
        series_map = self.storage.get_all_series_since(since)

        self._clear_legend()
        visible_series: list[tuple[str, QColor, list[tuple[datetime, float]]]] = []
        forecast_lines: list[str] = []
        eff_lines: list[str] = []

        for idx, ((pid, bid), pts) in enumerate(sorted(series_map.items())):
            color = SERIES_PALETTE[idx % len(SERIES_PALETTE)]
            label = f"{pid}: {bid}"
            cb = QCheckBox(label)
            cb.setChecked((pid, bid) not in self._disabled_keys)
            cb.setStyleSheet(f"QCheckBox {{ color: {color.name()}; }}")
            cb.toggled.connect(lambda checked, k=(pid, bid): self._toggle_series(k, checked))
            self.legend_layout.addWidget(cb)

            if (pid, bid) not in self._disabled_keys:
                visible_series.append((label, color, pts))

            hrs_left = compute_burn_rate_hours(pts)
            if hrs_left is not None:
                h_i = int(hrs_left)
                m_i = int((hrs_left - h_i) * 60)
                forecast_lines.append(f"• {label}: ~{h_i}h {m_i}m until 0% at current pace")

            cycles, avg_used = compute_reset_cycle_efficiency(pts)
            if cycles > 0 and avg_used is not None:
                eff_lines.append(f"• {label}: avg {avg_used * 100:.1f}% used across {cycles} reset cycle(s)")

        self.chart.set_series(visible_series, since, now)
        self.forecast_label.setText("\n".join(forecast_lines) if forecast_lines else "No active downward burn rate detected in this window.")
        self.heatmap.set_grid(compute_hourly_heatmap(series_map))
        self.eff_label.setText("\n".join(eff_lines) if eff_lines else "No completed reset cycles recorded in this window yet.")

        tok_rows = self.storage.get_token_usage_summary(since)
        if tok_rows:
            tok_lines = [
                f"• {pid} / {model}: {inp:,} in + {out:,} out ({cached:,} cached)"
                for pid, model, inp, out, cached in tok_rows
            ]
            self.token_label.setText("\n".join(tok_lines))
        else:
            self.token_label.setText("No token events recorded yet (ready for local CLI log ingestion).")

    def _toggle_series(self, key: tuple[str, str], checked: bool) -> None:
        if checked:
            self._disabled_keys.discard(key)
        else:
            self._disabled_keys.add(key)
        self.refresh()
