from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ai_usage_tray.core.models import TokenUsageRecord
from ai_usage_tray.core.storage import HistoryStorage
from ai_usage_tray.ui.analytics import (
    compute_burn_rate_hours,
    compute_hourly_heatmap,
    compute_reset_cycle_efficiency,
)


def test_compute_burn_rate_hours():
    t0 = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
    pts = [
        (t0, 1.0),
        (t0 + timedelta(hours=1), 0.75),
    ]
    hrs = compute_burn_rate_hours(pts)
    assert hrs is not None
    assert abs(hrs - 3.0) < 1e-6


def test_compute_reset_cycle_efficiency():
    t0 = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
    pts = [
        (t0, 1.0),
        (t0 + timedelta(hours=1), 0.6),
        (t0 + timedelta(hours=2), 1.0),  # reset jump (+0.4)
        (t0 + timedelta(hours=3), 0.8),
        (t0 + timedelta(hours=4), 1.0),  # reset jump (+0.2)
    ]
    cycles, avg_used = compute_reset_cycle_efficiency(pts)
    assert cycles == 2
    assert avg_used is not None
    assert abs(avg_used - 0.3) < 1e-6


def test_compute_hourly_heatmap_and_storage(tmp_path):
    t0 = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
    grid = compute_hourly_heatmap({("ag", "5h"): [(t0, 0.9), (t0 + timedelta(minutes=30), 0.7)]})
    local_dt = (t0 + timedelta(minutes=30)).astimezone()
    assert abs(grid[local_dt.weekday()][local_dt.hour] - 0.2) < 1e-6

    store = HistoryStorage(db_path=str(tmp_path / "h.db"))
    store.record_token_usage(
        TokenUsageRecord(
            timestamp=t0,
            provider_id="openai_codex",
            model="gpt-5",
            input_tokens=100,
            output_tokens=50,
            cached_tokens=20,
            cost_usd=Decimal("0.01"),
        )
    )
    rows = store.get_token_usage_summary(t0 - timedelta(hours=1))
    assert rows == [("openai_codex", "gpt-5", 100, 50, 20)]


def test_forecast_respects_reset_horizon():
    from ai_usage_tray.ui.analytics import BurnRateDetails, forecast_status

    now = datetime.now(timezone.utc)
    fast = BurnRateDetails(20, 0.5, 1, 3)
    slow = BurnRateDetails(1, 99 * 24, 1, 3)
    reset = now + timedelta(hours=2)
    assert forecast_status(fast, reset, now) == ("~30m · May run out before reset", True)
    assert forecast_status(slow, reset, now) == ("Reset expected first", False)
    assert forecast_status(fast, now - timedelta(minutes=1), now) == ("Awaiting reset", False)
    assert forecast_status(fast, None, now) == ("~30m · Reset unknown", False)
    assert forecast_status(None, reset, now) == ("Insufficient history", False)
    assert forecast_status(BurnRateDetails(0, None, 1, 3), reset, now) == ("Steady", False)
    # Providers may return naive UTC datetimes.
    assert forecast_status(fast, reset.replace(tzinfo=None), now)[1]


def test_burn_rate_units_and_small_values():
    from ai_usage_tray.ui.analytics import format_burn_rate

    assert format_burn_rate(None) == "Insufficient history"
    assert format_burn_rate(0) == "0.0 pp/h"
    assert format_burn_rate(0.003) == "<0.1 pp/h"
    assert format_burn_rate(5.53) == "5.5 pp/h"


def test_dashboard_filters_and_resizes(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QLabel
    from ai_usage_tray.demo import build_demo_snapshots
    from ai_usage_tray.ui.analytics import AnalyticsTabView

    app = QApplication.instance() or QApplication([])
    store = HistoryStorage(str(tmp_path / "dashboard.db"))
    snapshots = build_demo_snapshots()
    now = datetime.now(timezone.utc)
    for snapshot in snapshots:
        snapshot.fetched_at = now - timedelta(hours=1)
        store.record_snapshot(snapshot)
        snapshot.fetched_at = now
        store.record_snapshot(snapshot)
    store.record_token_usage(TokenUsageRecord(now, "openai_codex", "model", 10, 5))
    view = AnalyticsTabView(store, snapshots={s.provider_id: s for s in snapshots})
    view.resize(900, 850)
    view.refresh()
    view.show()
    app.processEvents()
    try:
        assert view.provider_combo.currentData() == "antigravity"
        assert len(view.chart._series) == 5
        assert view.token_section.isHidden()
        assert view.patterns.isHidden()
        assert view.selector.isHidden()
        key = view._all_keys[0]
        view._toggle_key_flip(key)
        assert len(view.chart._series) == 4
        view._clear_all_stats()
        assert view.chart._series == []
        assert all(v == 0 for row in view.heatmap._grid for v in row)
        view._select_all_stats()
        assert len(view.chart._series) == 5
        view.focus_series("openai_codex", "codex-primary")
        assert view.provider_combo.currentData() == "openai_codex"
        assert len(view.chart._series) == 1
        assert "5-hour" in view.chart._series[0][0]
        assert not view.token_section.isHidden()
        view._toggle_details(("openai_codex", "codex-primary"))
        assert any("1 year:" in label.text() for label in view.findChildren(QLabel))
        view.provider_combo.setCurrentIndex(view.provider_combo.findData("*"))
        view._select_all_stats()
        assert len(view.chart._series) == 15
        view.choose_btn.click()
        view.patterns_btn.click()
        assert not view.selector.isHidden()
        assert not view.patterns.isHidden()
        for width in (900, 680, 480, 380):
            view.resize(width, 850)
            for _ in range(3):
                app.processEvents()
            assert view.width() == width
            assert view.scroll.horizontalScrollBar().maximum() == 0
            assert view.scroll.widget().width() <= view.scroll.viewport().width()
            for row in range(view.forecast_grid.count()):
                frame = view.forecast_grid.itemAt(row).widget()
                assert frame.geometry().right() <= view.scroll.widget().width()
        view.range_combo.setCurrentIndex(view.range_combo.findData(-1))
        view.custom_spin.setValue(2)
        view.custom_unit.setCurrentIndex(1)
        assert view._selected_hours() == 48
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()


def test_dashboard_without_history(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from ai_usage_tray.ui.analytics import AnalyticsTabView

    app = QApplication.instance() or QApplication([])
    view = AnalyticsTabView(HistoryStorage(str(tmp_path / "empty.db")))
    view.refresh()
    assert not view.chart._series
    assert view.forecast_grid.count() == 1
    assert view.token_section.isHidden()
    view.deleteLater()
    app.processEvents()
