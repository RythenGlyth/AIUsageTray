from datetime import datetime, timedelta, timezone

from ai_usage_tray.core.storage import HistoryStorage
from ai_usage_tray.demo import build_demo_snapshots, seed_demo_history


def test_demo_uses_observed_groups_and_mock_claude_subscription():
    now = datetime(2026, 12, 3, 10, tzinfo=timezone.utc)
    snapshots = {s.provider_id: s for s in build_demo_snapshots(now)}
    antigravity = snapshots['antigravity']
    assert [[b.bucket_id for b in g.buckets] for g in antigravity.groups] == [
        ['gemini-weekly', 'gemini-5h'], ['3p-weekly', '3p-5h'], ['gemini-argon-wtu-weekly'],
    ]
    assert {b.bucket_id for b in snapshots['openai_codex'].all_buckets} == {'codex-primary', 'codex-secondary'}
    claude = snapshots['claude']
    assert claude.plan_name == 'Pro'
    assert {b.bucket_id for b in claude.all_buckets} == {'claude-five_hour', 'claude-seven_day', 'claude-seven_day_sonnet'}
    copilot = snapshots['github_copilot']
    assert [b.bucket_id for b in copilot.all_buckets] == ['copilot-premium_interactions']
    assert copilot.badges == ['Chat: Unlimited', 'Completions: Unlimited']
    assert copilot.all_buckets[0].reset_time == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_demo_history_matches_quota_values_and_reset_boundaries(tmp_path):
    now = datetime(2026, 12, 3, 10, tzinfo=timezone.utc)
    snapshots = build_demo_snapshots(now)
    store = HistoryStorage(str(tmp_path / 'demo.db'))
    seed_demo_history(store, snapshots, now)
    series = store.get_all_series_since(now - timedelta(days=8))
    assert len(series) == 15
    for snap in snapshots:
        for bucket in snap.all_buckets:
            points = series[snap.provider_id, bucket.bucket_id]
            assert len(points) == 337
            assert points[-1][0] == now
            assert abs(points[-1][1] - bucket.remaining_float) < 1e-7
            resets = [(t, v) for (_, prev), (t, v) in zip(points, points[1:]) if v > prev]
            assert resets
            for t, value in resets:
                assert value == 1
                if bucket.window == 'monthly':
                    assert t.day == 1 and t.hour == 0 and t.minute == 0
                else:
                    seconds = (t - bucket.reset_time).total_seconds()
                    period = (5 if bucket.window == '5h' else 168) * 3600
                    assert seconds % period < 1800
