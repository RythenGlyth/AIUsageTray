from decimal import Decimal
from datetime import datetime, timezone

from ai_usage_tray.providers.gemini_web import parse_gemini_web_qpebw


def test_parse_gemini_web_qpebw_5h():
    now = datetime.fromtimestamp(1795269413, tz=timezone.utc)
    payload = [[[[None, 2], 2, 12, [1795285128, 283503000], 48384, 42395]], ""]
    snap = parse_gemini_web_qpebw(payload, now=now)
    assert snap.provider_id == "gemini_web"
    buckets = snap.all_buckets
    assert len(buckets) == 1
    assert buckets[0].window == "5h"
    assert buckets[0].remaining_fraction == Decimal("42395") / Decimal("48384")
