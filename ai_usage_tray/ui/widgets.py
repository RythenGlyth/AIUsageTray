from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ai_usage_tray.core.models import QuotaBucket, format_countdown


def status_color_for_fraction(fraction: float) -> QColor:
    if fraction >= 0.50:
        return QColor("#2ecc71")  # Emerald green
    if fraction >= 0.20:
        return QColor("#f39c12")  # Amber
    return QColor("#e74c3c")  # Red


class RingGaugeWidget(QWidget):
    """Pure-QPainter circular progress ring showing remaining capacity."""

    def __init__(self, size: int = 52, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._fraction: Optional[float] = None
        self._label: str = "—"
        self.setFixedSize(size, size)

    def set_value(self, fraction: Optional[float], label: str) -> None:
        self._fraction = fraction
        self._label = label
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        margin = 5.0
        rect = QRectF(margin, margin, self.width() - 2 * margin, self.height() - 2 * margin)

        track_pen = QPen(QColor(128, 128, 128, 45), 4.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.drawEllipse(rect)

        if self._fraction is not None:
            clamped = max(0.0, min(1.0, self._fraction))
            arc_pen = QPen(status_color_for_fraction(clamped), 4.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            painter.setPen(arc_pen)
            span_angle = int(-360.0 * clamped * 16.0)
            painter.drawArc(rect, 90 * 16, span_angle)

        painter.setPen(self.palette().text().color())
        font = QFont(self.font())
        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._label)


class MiniSparkline(QWidget):
    """Compact historical trend sparkline drawn with QPainter."""

    def __init__(self, values: list[float], color: QColor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._values = values
        self._color = color
        self.setFixedSize(68, 18)

    def paintEvent(self, event) -> None:  # noqa: N802
        if len(self._values) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = float(self.width() - 4)
        h = float(self.height() - 4)
        n = len(self._values)

        path = QPainterPath()
        for i, val in enumerate(self._values):
            x = 2.0 + (i / (n - 1)) * w
            y = 2.0 + (1.0 - max(0.0, min(1.0, val))) * h
            pt = QPointF(x, y)
            if i == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)

        pen = QPen(self._color, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)


class QuotaBucketCard(QFrame):
    """Clean, spacious card for a single quota bucket with full-precision percentage and countdown."""

    def __init__(
        self,
        bucket: QuotaBucket,
        history: Optional[list[tuple[datetime, float]] | list[float]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("QuotaCard")
        self.setStyleSheet(
            """
            QFrame#QuotaCard {
                background-color: rgba(128, 128, 128, 0.06);
                border: 1px solid rgba(128, 128, 128, 0.16);
                border-radius: 10px;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(7)

        # Top row: Name + Exact %
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        name_label = QLabel(bucket.display_name)
        name_font = QFont(name_label.font())
        name_font.setBold(True)
        name_font.setPointSize(9)
        name_label.setFont(name_font)
        top_row.addWidget(name_label, 1)

        pct_label = QLabel(bucket.remaining_percent_str)
        pct_font = QFont("monospace")
        pct_font.setStyleHint(QFont.StyleHint.Monospace)
        pct_font.setBold(True)
        pct_font.setPointSize(10)
        pct_label.setFont(pct_font)
        color = status_color_for_fraction(bucket.remaining_float)
        pct_label.setStyleSheet(f"color: {color.name()};")
        top_row.addWidget(pct_label, 0, Qt.AlignmentFlag.AlignRight)

        layout.addLayout(top_row)

        # Slim 5px progress bar
        bar = QProgressBar()
        bar.setRange(0, 10000)
        bar.setValue(int(round(bucket.remaining_float * 10000)))
        bar.setTextVisible(False)
        bar.setFixedHeight(5)
        bar.setStyleSheet(
            f"""
            QProgressBar {{
                background-color: rgba(128, 128, 128, 0.18);
                border: none;
                border-radius: 2px;
            }}
            QProgressBar::chunk {{
                background-color: {color.name()};
                border-radius: 2px;
            }}
            """
        )
        layout.addWidget(bar)

        # Bottom row: concise metadata + sparkline
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)

        meta_parts: list[str] = []
        if bucket.description and len(bucket.description) <= 45:
            meta_parts.append(bucket.description)
        meta_parts.append(format_countdown(bucket.reset_time))

        pace_warning = False
        spark: Optional[MiniSparkline] = None

        if history and len(history) >= 2:
            if isinstance(history[0], tuple):
                pts = history  # type: ignore[assignment]
                raw_values = [v for _, v in pts]
                if len(raw_values) > 60:
                    step = len(raw_values) / 60.0
                    values = [raw_values[int(i * step)] for i in range(59)] + [raw_values[-1]]
                else:
                    values = raw_values
                t_start = pts[0][0].astimezone().strftime("%b %d %H:%M")
                t_end = pts[-1][0].astimezone().strftime("%b %d %H:%M")
                if pts[0][0].astimezone().date() == pts[-1][0].astimezone().date():
                    t_end = pts[-1][0].astimezone().strftime("%H:%M")
                tooltip = (
                    f"Trend ({len(pts)} samples): {t_start} → {t_end}\n"
                    f"{raw_values[0] * 100:.2f}% → {raw_values[-1] * 100:.2f}%"
                )

                from ai_usage_tray.ui.analytics import compute_burn_rate_details, format_short_duration

                details = compute_burn_rate_details(pts)
                if details is not None and details.hours_to_empty is not None:
                    eta_str = format_short_duration(details.hours_to_empty)
                    if bucket.reset_time is not None:
                        hrs_to_reset = (bucket.reset_time - datetime.now(timezone.utc)).total_seconds() / 3600.0
                        if 0 < details.hours_to_empty < hrs_to_reset:
                            meta_parts.append(f"⚠ ~{eta_str} to 0% (-{details.rate_pct_per_hour:.1f}%/hr)")
                            pace_warning = True
                        else:
                            meta_parts.append(f"~{eta_str} to 0% (-{details.rate_pct_per_hour:.1f}%/hr)")
                    else:
                        meta_parts.append(f"~{eta_str} to 0% (-{details.rate_pct_per_hour:.1f}%/hr)")
            else:
                values = list(history)  # type: ignore[arg-type]
                tooltip = f"Recent quota trend ({len(values)} samples)"
            spark = MiniSparkline(values, color)
            spark.setToolTip(tooltip)

        reset_label = QLabel("  •  ".join(meta_parts))
        reset_font = QFont(reset_label.font())
        reset_font.setPointSize(8)
        reset_label.setFont(reset_font)
        reset_label.setStyleSheet("color: #f39c12;" if pace_warning else "color: rgba(160, 160, 160, 0.92);")
        if bucket.reset_time is not None:
            local_ts = bucket.reset_time.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            reset_label.setToolTip(f"Resets at {local_ts}")
        bottom_row.addWidget(reset_label, 1)

        if spark is not None:
            bottom_row.addWidget(spark, 0, Qt.AlignmentFlag.AlignRight)

        layout.addLayout(bottom_row)

        if bucket.description:
            self.setToolTip(bucket.description)
