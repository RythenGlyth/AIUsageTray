from decimal import Decimal

from ai_usage_tray.providers.github_copilot import parse_copilot_user_response


def test_parse_copilot_user_response():
    payload = {
        "copilot_plan": "individual",
        "quota_reset_date_utc": "2026-12-01T00:00:00.000Z",
        "quota_snapshots": {
            "premium_interactions": {
                "entitlement": 300,
                "remaining": 245,
                "percent_remaining": 81.6666,
                "unlimited": False,
            },
            "chat": {"unlimited": True},
        },
    }
    snap = parse_copilot_user_response(payload, source_label="gh CLI")
    assert snap.provider_id == "github_copilot"
    assert snap.plan_name == "Individual"
    assert "Chat: Unlimited" in snap.badges
    buckets = snap.all_buckets
    assert len(buckets) == 1
    assert buckets[0].remaining_fraction == Decimal("245") / Decimal("300")
