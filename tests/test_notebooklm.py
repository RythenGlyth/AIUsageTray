from decimal import Decimal

from ai_usage_tray.providers.notebooklm import (
    extract_tier_from_ozz5z,
    parse_batchexecute_rpc,
    parse_notebooklm_eyldcb,
)


SAMPLE_EYLDCB_RAW = """
)]}'

1168
[["wrb.fr","EylDcb","[1,[[null,null,null,null,2,[1795769707,776927000],null,100],[null,null,null,null,1,[1795272907,776746000],null,87.5]],null,[]]",null,null,null,"generic"]]
"""


def test_parse_notebooklm_eyldcb():
    data = parse_batchexecute_rpc(SAMPLE_EYLDCB_RAW, "EylDcb")
    assert data is not None
    snap = parse_notebooklm_eyldcb(data, tier_raw="NOTEBOOKLM_TIER_PRO_CONSUMER_USER")
    assert snap.provider_id == "notebooklm"
    assert snap.plan_name == "AI Pro"
    buckets = snap.all_buckets
    assert len(buckets) == 2
    assert buckets[0].window == "weekly"
    assert buckets[0].remaining_percent_str == "100%"
    assert buckets[1].window == "5h"
    assert buckets[1].remaining_fraction == Decimal("0.875")
    assert buckets[1].remaining_percent_str == "87.5%"


def test_extract_tier_from_ozz5z():
    payload = [[[[None, "1", 1469], [], None, 3570, 3570, 1601, "NOTEBOOKLM_TIER_PRO_CONSUMER_USER"]]]
    assert extract_tier_from_ozz5z(payload) == "NOTEBOOKLM_TIER_PRO_CONSUMER_USER"
