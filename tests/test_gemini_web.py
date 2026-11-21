from decimal import Decimal
from datetime import datetime, timezone

from ai_usage_tray.providers.gemini_web import parse_gemini_web_graphql, parse_gemini_web_qpebw


def test_parse_gemini_web_graphql():
    payload = [
        {
            "data": {
                "viewer": {
                    "rpcs": {
                        "getUsageInfo": {
                            "subscriptionTier": "TIER1",
                            "pools": [
                                {
                                    "poolType": "GXU_WEEKLY",
                                    "percentageUsed": 0.12393263,
                                    "gxu": {"windowRefreshesAt": "2026-11-24T07:18:48.283643Z"},
                                },
                                {
                                    "poolType": "GXU_FIVE_HOURLY",
                                    "percentageUsed": 0.01,
                                    "gxu": {"windowRefreshesAt": "2026-11-21T18:18:48.283503Z"},
                                },
                            ],
                        }
                    }
                }
            }
        }
    ]
    snap = parse_gemini_web_graphql(payload)
    assert snap is not None
    assert snap.plan_name == "Tier 1"
    buckets = snap.all_buckets
    assert len(buckets) == 2
    assert buckets[0].window == "weekly"
    assert buckets[0].remaining_percent_str == "87.606737%"
    assert buckets[1].window == "5h"
    assert buckets[1].remaining_percent_str == "99%"


def test_parse_gemini_web_qpebw_5h():
    now = datetime.fromtimestamp(1795269413, tz=timezone.utc)
    payload = [[[[None, 2], 2, 12, [1795285128, 283503000], 48384, 42395]], ""]
    snap = parse_gemini_web_qpebw(payload, now=now)
    assert snap.provider_id == "gemini_web"
    buckets = snap.all_buckets
    assert len(buckets) == 1
    assert buckets[0].window == "5h"
    assert buckets[0].remaining_fraction == Decimal("42395") / Decimal("48384")
