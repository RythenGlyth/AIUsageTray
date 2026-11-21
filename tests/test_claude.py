from decimal import Decimal
from unittest.mock import MagicMock, patch

from ai_usage_tray.core.config import ConfigManager
from ai_usage_tray.providers.claude import ClaudeProvider, parse_claude_usage_payload


def test_parse_claude_usage_payload():
    payload = {
        "five_hour": {"utilization": 14.5, "resets_at": "2026-11-21T18:00:00Z"},
        "seven_day": {"utilization": 32.0, "resets_at": "2026-11-28T12:00:00Z"},
        "seven_day_sonnet": {"utilization": 0.0, "resets_at": "2026-11-28T12:00:00Z"},
        "extra_usage": {"is_enabled": True, "used_credits": 5, "monthly_limit": 50},
    }
    snap = parse_claude_usage_payload(payload, source_label="OAuth")
    assert snap.provider_id == "claude"
    assert len(snap.all_buckets) == 3
    assert snap.all_buckets[0].remaining_fraction == Decimal("0.855")
    assert snap.all_buckets[0].remaining_percent_str == "85.5%"
    assert "Extra: 5/50" in snap.badges


def test_claude_429_backoff(tmp_path):
    cfg = ConfigManager(config_dir=str(tmp_path))
    provider = ClaudeProvider(cfg)

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "120"}

    with (
        patch.object(provider, "_resolve_oauth_tokens", return_value=(MagicMock(access_token="tok", refresh_token="", is_expired=lambda: False), False)),
        patch("ai_usage_tray.providers.claude.requests.get", return_value=mock_resp),
    ):
        snap = provider._fetch_via_oauth()
        assert snap is not None
        assert snap.rate_limited_until is not None
        assert "429" in (snap.error or "")
