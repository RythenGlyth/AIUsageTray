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
