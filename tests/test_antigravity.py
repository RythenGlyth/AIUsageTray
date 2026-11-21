from decimal import Decimal

from ai_usage_tray.providers.antigravity import (
    parse_ps_aux_for_antigravity,
    parse_retrieve_user_quota_summary,
    parse_ss_ports_for_pid,
)


SAMPLE_PS_AUX = """
user      943616  0.0  0.3 2719548 201036 ?      Sl   Nov20   0:36 /opt/Antigravity/resources/bin/language_server --standalone --override_ide_name antigravity --https_server_port 0 --csrf_token c65640f6-7579-4ff5-804d-7a6020c3a502 --app_data_dir antigravity
"""

SAMPLE_SS = """
LISTEN 0      0           127.0.0.1:41093      0.0.0.0:*    users:(("language_server",pid=943616,fd=21))
LISTEN 0      0           127.0.0.1:33069      0.0.0.0:*    users:(("language_server",pid=943616,fd=24))
"""

SAMPLE_QUOTA_JSON = {
    "response": {
        "groups": [
            {
                "displayName": "Gemini Models",
                "description": "Models within this group: Gemini 4 Neon, Gemini 4 Hydrogen, Gemini 3.8 Flash",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "displayName": "Weekly Limit Remaining",
                        "window": "weekly",
                        "remainingFraction": 0.9968221,
                        "resetTime": "2026-11-28T11:20:04Z",
                    },
                    {
                        "bucketId": "gemini-5h",
                        "displayName": "Five Hour Limit Remaining",
                        "window": "5h",
                        "remainingFraction": 0.9888774,
                        "resetTime": "2026-11-21T16:20:04Z",
                    },
                ],
            },
            {
                "displayName": "Claude and GPT models",
                "buckets": [
                    {"bucketId": "3p-weekly", "displayName": "Weekly Limit Remaining", "window": "weekly", "remainingFraction": 1, "resetTime": "2026-11-28T12:34:59Z"},
                    {"bucketId": "3p-5h", "displayName": "Five Hour Limit Remaining", "window": "5h", "remainingFraction": 1, "resetTime": "2026-11-21T17:34:59Z"},
                ],
            },
            {
                "displayName": "Gemini 4 Argon",
                "buckets": [
                    {"bucketId": "gemini-argon-wtu-weekly", "displayName": "Gemini 4 Argon Weekly Limit", "window": "weekly", "remainingFraction": 1, "resetTime": "2026-11-28T12:34:59Z"},
                ],
            },
        ]
    }
}


def test_parse_ps_aux_and_ss():
    servers = parse_ps_aux_for_antigravity(SAMPLE_PS_AUX)
    assert len(servers) == 1
    assert servers[0].pid == 943616
    assert servers[0].csrf_token == "c65640f6-7579-4ff5-804d-7a6020c3a502"
    assert servers[0].explicit_port is None

    ports = parse_ss_ports_for_pid(SAMPLE_SS, 943616)
    assert ports == [41093, 33069]


def test_parse_retrieve_user_quota_summary_5_buckets():
    snap = parse_retrieve_user_quota_summary(SAMPLE_QUOTA_JSON)
    assert snap.provider_id == "antigravity"
    assert len(snap.groups) == 3
    buckets = snap.all_buckets
    assert len(buckets) == 5
    assert buckets[0].remaining_fraction == Decimal("0.9968221")
    assert buckets[0].remaining_percent_str == "99.68221%"
    assert buckets[1].remaining_percent_str == "98.88774%"
    assert buckets[2].remaining_percent_str == "100%"
