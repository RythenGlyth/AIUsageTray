from datetime import datetime, timezone
from decimal import Decimal

from ai_usage_tray.core.models import (
    ProviderUsageSnapshot,
    QuotaBucket,
    QuotaGroup,
    TokenUsageRecord,
    format_countdown,
    format_remaining_percent,
)
from ai_usage_tray.core.provider import AuthStatus, ProviderRegistry, UsageProvider
from ai_usage_tray.core.storage import HistoryStorage


def test_format_remaining_percent_preserves_full_precision():
    assert format_remaining_percent(Decimal("0.9968221")) == "99.68221%"
    assert format_remaining_percent(Decimal("0.9888774")) == "98.88774%"
    assert format_remaining_percent(Decimal("1")) == "100%"
    assert format_remaining_percent(Decimal("0.5")) == "50%"
    assert format_remaining_percent(Decimal("0")) == "0%"
    assert format_remaining_percent(Decimal("1.2")) == "100%"
    assert format_remaining_percent(Decimal("-0.1")) == "0%"


def test_format_countdown():
    now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
    later = datetime(2026, 11, 21, 15, 45, 0, tzinfo=timezone.utc)
    assert format_countdown(later, now=now) == "Resets in 3h 45m"
    past = datetime(2026, 11, 21, 11, 0, 0, tzinfo=timezone.utc)
    assert format_countdown(past, now=now) == "Resetting now"


def test_sqlite_storage_and_registry(tmp_path):
    db_file = str(tmp_path / "history.db")
    store = HistoryStorage(db_path=db_file)

    snap = ProviderUsageSnapshot(
        provider_id="antigravity",
        provider_name="Antigravity",
        source_label="Local Language Server",
        groups=[
            QuotaGroup(
                group_id="g1",
                display_name="Gemini Models",
                buckets=[
                    QuotaBucket(
                        bucket_id="gemini-5h",
                        display_name="5h",
                        window="5h",
                        remaining_fraction=Decimal("0.9888774"),
                    )
                ],
            )
        ],
    )
    store.record_snapshot(snap)
    hist = store.get_bucket_history("antigravity", "gemini-5h")
    assert len(hist) == 1
    assert abs(hist[0] - 0.9888774) < 1e-7

    store.record_token_usage(
        TokenUsageRecord(
            timestamp=datetime.now(timezone.utc),
            provider_id="openai_codex",
            model="gpt-5",
            input_tokens=100,
            output_tokens=50,
            cost_usd=Decimal("0.0025"),
        )
    )

    class DummyProvider(UsageProvider):
        provider_id = "dummy"
        display_name = "Dummy"

        def fetch_usage(self):
            return snap

        def get_auth_status(self):
            return AuthStatus(True, "ok")

    reg = ProviderRegistry()
    reg.register(DummyProvider())
    assert reg.get("dummy") is not None
    assert len(reg.all()) == 1
