from decimal import Decimal

from ai_usage_tray.providers.openai_codex import (
    parse_codex_app_server_result,
    parse_wham_usage_response,
)


SAMPLE_APP_SERVER_RESULT = {
    "rateLimits": {
        "limitId": "codex",
        "primary": {"usedPercent": 12.5, "windowDurationMins": 300, "resetsAt": 1795283693},
        "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 1795870493},
        "credits": {"hasCredits": True, "unlimited": False, "balance": "42.50"},
        "planType": "plus",
    },
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "primary": {"usedPercent": 12.5, "windowDurationMins": 300, "resetsAt": 1795283693},
            "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 1795870493},
            "credits": {"hasCredits": True, "unlimited": False, "balance": "42.50"},
            "planType": "plus",
        }
    },
    "rateLimitResetCredits": {"availableCount": 3},
}


def test_parse_codex_app_server_result():
    snap = parse_codex_app_server_result(SAMPLE_APP_SERVER_RESULT)
    assert snap.provider_id == "openai_codex"
    assert snap.source_label == "Codex Binary"
    assert snap.plan_name == "Plus"
    assert "Credits: 42.50" in snap.badges
    assert "3 Reset Credits" in snap.badges
    buckets = snap.all_buckets
    assert len(buckets) == 2
    assert buckets[0].remaining_fraction == Decimal("0.875")
    assert buckets[0].remaining_percent_str == "87.5%"
    assert buckets[1].remaining_percent_str == "100%"


def test_parse_wham_usage_response():
    payload = {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {"used_percent": 25, "limit_window_seconds": 18000, "reset_at": 1795283693},
            "secondary_window": {"used_percent": 10, "limit_window_seconds": 604800, "reset_at": 1795870493},
        },
    }
    snap = parse_wham_usage_response(payload)
    assert snap.source_label == "OAuth"
    assert snap.plan_name == "Pro"
    assert snap.all_buckets[0].remaining_percent_str == "75%"
    assert snap.all_buckets[1].remaining_percent_str == "90%"
