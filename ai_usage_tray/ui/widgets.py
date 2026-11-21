from __future__ import annotations

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

    def __init__(self, size: int = 56, parent: Optional[QWidget] = None) -> None:
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

        track_pen = QPen(QColor(128, 128, 128, 55), 5.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.drawEllipse(rect)

        if self._fraction is not None:
            clamped = max(0.0, min(1.0, self._fraction))
            arc_pen = QPen(status_color_for_fraction(clamped), 5.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
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
        self.setFixedSize(72, 18)

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
    """Visual card for a single quota bucket with full-precision percentage and countdown."""

    def __init__(
        self,
        bucket: QuotaBucket,
        history: Optional[list[float]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            """
            QuotaBucketCard {
                background-color: palette(base);
                border: 1px solid rgba(128, 128, 128, 0.25);
                border-radius: 8px;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)

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
        pct_font.setPointSize(9)
        pct_label.setFont(pct_font)
        color = status_color_for_fraction(bucket.remaining_float)
        pct_label.setStyleSheet(f"color: {color.name()};")
        top_row.addWidget(pct_label, 0, Qt.AlignmentFlag.AlignRight)

        layout.addLayout(top_row)

        bar = QProgressBar()
        bar.setRange(0, 10000)
        bar.setValue(int(round(bucket.remaining_float * 10000)))
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        bar.setStyleSheet(
            f"""
            QProgressBar {{
                background-color: rgba(128, 128, 128, 0.20);
                border: none;
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background-color: {color.name()};
                border-radius: 4px;
            }}
            """
        )
        layout.addWidget(bar)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(6)

        countdown_text = format_countdown(bucket.reset_time)
        if bucket.description and len(bucket.description) <= 45:
            countdown_text = f"{bucket.description}  •  {countdown_text}"
        reset_label = QLabel(countdown_text)
        reset_font = QFont(reset_label.font())
        reset_font.setPointSize(8)
        reset_label.setFont(reset_font)
        reset_label.setStyleSheet("color: rgba(160, 160, 160, 0.95);")
        if bucket.reset_time is not None:
            local_ts = bucket.reset_time.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            reset_label.setToolTip(f"Resets at {local_ts}")
        bottom_row.addWidget(reset_label, 1)

        if history and len(history) >= 2:
            spark = MiniSparkline(history, color)
            spark.setToolTip("Recent quota trend")
            bottom_row.addWidget(spark, 0, Qt.AlignmentFlag.AlignRight)

        layout.addLayout(bottom_row)

        if bucket.description:
            self.setToolTip(bucket.description)
