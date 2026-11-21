from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ai_usage_tray.core.models import ProviderUsageSnapshot, QuotaBucket, QuotaGroup
from ai_usage_tray.core.provider import AuthStatus, ProviderRegistry, UsageProvider
from ai_usage_tray.core.storage import HistoryStorage


class DemoUsageProvider(UsageProvider):
    """Static mock provider used by `--demo` mode for screenshots and UI testing."""

    supports_oauth = False
    supports_token_tracking = False
    default_poll_interval_s = 300

    def __init__(self, snapshot: ProviderUsageSnapshot) -> None:
        self.provider_id = snapshot.provider_id
        self.display_name = snapshot.provider_name
        self._snapshot = snapshot

    def fetch_usage(self) -> ProviderUsageSnapshot:
        return ProviderUsageSnapshot(
            provider_id=self._snapshot.provider_id,
            provider_name=self._snapshot.provider_name,
            source_label=self._snapshot.source_label,
            fetched_at=datetime.now(timezone.utc),
            groups=self._snapshot.groups,
            plan_name=self._snapshot.plan_name,
            badges=list(self._snapshot.badges),
        )

    def get_auth_status(self) -> AuthStatus:
        return AuthStatus(True, f"Demo ({self._snapshot.source_label})")


def build_demo_snapshots(now: datetime | None = None) -> list[ProviderUsageSnapshot]:
    """Live-observed quota structure (2026-11-21), with illustrative usage values.

    Names, IDs, grouping, windows, plans and unlimited/empty states reflect the
    connected accounts at inspection time; they are not universal plan promises.
    Claude deliberately simulates a subscribed Pro account instead of the live
    account's empty response, so its limits remain demonstrable.
    No credentials, account IDs, raw responses or personal usage are embedded.
    """
    cur = now or datetime.now(timezone.utc)
    next_month = cur.replace(year=cur.year + (cur.month == 12), month=cur.month % 12 + 1,
                             day=1, hour=0, minute=0, second=0, microsecond=0)

    def bucket(bid: str, name: str, window: str, remaining: str,
               reset_hours: float, description: str = "") -> QuotaBucket:
        return QuotaBucket(bid, name, window, Decimal(remaining),
                           cur + timedelta(hours=reset_hours), description)

    return [
        ProviderUsageSnapshot(
            provider_id="antigravity", provider_name="Antigravity",
            source_label="Local Language Server", fetched_at=cur,
            groups=[
                QuotaGroup(
                    group_id="ag-group-0", display_name="Gemini Models",
                    description="Models within this group: Gemini 4 Neon, Gemini 4 Hydrogen, Gemini 3.8 Flash",
                    buckets=[
                        bucket("gemini-weekly", "Weekly Limit Remaining", "weekly", "0.7316425", 160),
                        bucket("gemini-5h", "Five Hour Limit Remaining", "5h", "0.3582147", 2.1),
                    ],
                ),
                QuotaGroup(
                    group_id="ag-group-1", display_name="Claude and GPT models",
                    description="Models within this group: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B",
                    buckets=[
                        bucket("3p-weekly", "Weekly Limit Remaining", "weekly", "0.84", 100),
                        bucket("3p-5h", "Five Hour Limit Remaining", "5h", "0.6184", 4.0),
                    ],
                ),
                QuotaGroup(
                    group_id="ag-group-2", display_name="Gemini 4 Argon",
                    description="For paid Pro plans, Argon quota is separate. Higher Argon quota (included as part of the larger Gemini quota bucket) is available on Ultra plans.",
                    buckets=[bucket("gemini-argon-wtu-weekly", "Gemini 4 Argon Weekly Limit", "weekly", "0.91", 120)],
                ),
            ],
        ),
        ProviderUsageSnapshot(
            provider_id="openai_codex", provider_name="Codex (OpenAI)",
            source_label="Codex Binary", plan_name="Plus", fetched_at=cur,
            badges=["3 Reset Credits"],
            groups=[QuotaGroup(
                group_id="codex-codex", display_name="Codex",
                buckets=[
                    bucket("codex-primary", "5-Hour Limit Remaining", "5h", "0.425", 2.25),
                    bucket("codex-secondary", "Weekly Limit Remaining", "weekly", "0.78", 102),
                ],
            )],
        ),
        ProviderUsageSnapshot(
            provider_id="claude", provider_name="Claude", source_label="Desktop Cookies",
            plan_name="Pro", fetched_at=cur,
            groups=[QuotaGroup(
                group_id="claude-limits", display_name="Claude Usage Limits",
                buckets=[
                    bucket("claude-five_hour", "5-Hour Limit Remaining", "5h", "0.18", 1.1),
                    bucket("claude-seven_day", "Weekly Limit Remaining", "weekly", "0.64", 83),
                    bucket("claude-seven_day_sonnet", "Sonnet Weekly Limit Remaining", "weekly", "0.72", 83),
                ],
            )],
        ),
        ProviderUsageSnapshot(
            provider_id="notebooklm", provider_name="NotebookLM", source_label="Browser Session",
            fetched_at=cur,
            groups=[QuotaGroup(
                group_id="notebooklm-limits", display_name="NotebookLM Usage",
                buckets=[
                    bucket("notebooklm-5h", "5-Hour Limit Remaining", "5h", "0.88", 3.8),
                    bucket("notebooklm-weekly", "Weekly Limit Remaining", "weekly", "0.94", 122),
                ],
            )],
        ),
        ProviderUsageSnapshot(
            provider_id="gemini_web", provider_name="Gemini Web", source_label="Browser GraphQL",
            plan_name="Tier 1", fetched_at=cur,
            groups=[QuotaGroup(
                group_id="gemini-web-limits", display_name="Gemini Web Usage",
                buckets=[
                    bucket("gemini-web-weekly", "Weekly Limit Remaining", "weekly", "0.89", 114),
                    bucket("gemini-web-5h", "5-Hour Limit Remaining", "5h", "0.74", 2.75),
                ],
            )],
        ),
        ProviderUsageSnapshot(
            provider_id="github_copilot", provider_name="GitHub Copilot", source_label="gh CLI",
            plan_name="Individual", fetched_at=cur,
            badges=["Chat: Unlimited", "Completions: Unlimited"],
            groups=[QuotaGroup(
                group_id="copilot-quotas", display_name="GitHub Copilot Quotas",
                buckets=[QuotaBucket(
                    bucket_id="copilot-premium_interactions", display_name="Premium Interactions Remaining",
                    window="monthly", remaining_fraction=Decimal("0.565"), reset_time=next_month,
                    description="113 / 200 remaining",
                )],
            )],
        ),
    ]


def seed_demo_history(storage: HistoryStorage, snapshots: list[ProviderUsageSnapshot], now: datetime | None = None) -> None:
    """Seed seven days of synthetic consumption aligned to each quota's resets."""
    import random

    rng = random.Random(42)
    cur = now or datetime.now(timezone.utc)
    times = [cur - timedelta(minutes=30 * (336 - i)) for i in range(337)]
    for snap in snapshots:
        for bucket in snap.all_buckets:
            period = timedelta(hours=5) if bucket.window == "5h" else timedelta(days=7)
            cycle_start = (bucket.reset_time or cur) - period

            def cycle_key(ts: datetime):
                if bucket.window == "monthly":
                    return ts.year, ts.month
                return math.floor((ts - cycle_start).total_seconds() / period.total_seconds())

            # Group sample times by reset cycle. Monthly quotas reset on calendar
            # boundaries; five-hour and weekly windows use their own reset anchor.
            cycles: list[list[datetime]] = []
            for ts in times:
                if not cycles or cycle_key(ts) != cycle_key(cycles[-1][-1]):
                    cycles.append([])
                cycles[-1].append(ts)
            for cycle_idx, timestamps in enumerate(cycles):
                target = bucket.remaining_float if cycle_idx == len(cycles) - 1 else rng.uniform(0.15, 0.85)
                weights = []
                for ts in timestamps[1:]:
                    local = ts.astimezone()
                    probability = 0.6 if 9 <= local.hour <= 22 else 0.12
                    if local.weekday() >= 5:
                        probability *= 0.65
                    weights.append(rng.uniform(0.4, 1.5) if rng.random() < probability else 0.0)
                if weights and sum(weights) == 0:
                    weights[-1] = 1.0
                total_weight = sum(weights) or 1.0
                consumed_weight = 0.0
                for idx, ts in enumerate(timestamps):
                    if idx:
                        consumed_weight += weights[idx - 1]
                    fraction = 1.0 - (1.0 - target) * consumed_weight / total_weight
                    if idx == len(timestamps) - 1:
                        fraction = target
                    storage.record_snapshot(ProviderUsageSnapshot(
                        provider_id=snap.provider_id, provider_name=snap.provider_name,
                        source_label=snap.source_label, fetched_at=ts,
                        groups=[QuotaGroup(
                            group_id="demo", display_name="Demo history",
                            buckets=[QuotaBucket(
                                bucket_id=bucket.bucket_id, display_name=bucket.display_name,
                                window=bucket.window, remaining_fraction=Decimal(str(round(fraction, 7))),
                            )],
                        )],
                    ))


def register_demo_providers(registry: ProviderRegistry, storage: HistoryStorage) -> None:
    now = datetime.now(timezone.utc)
    snapshots = build_demo_snapshots(now)
    seed_demo_history(storage, snapshots, now)
    for snap in snapshots:
        registry.register(DemoUsageProvider(snap))


def _wait_and_flush(app, delay_ms: int = 180) -> None:
    from PyQt6.QtCore import QEvent, QEventLoop, QTimer

    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    loop = QEventLoop()
    QTimer.singleShot(delay_ms, loop.quit)
    loop.exec()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def _render_widget_hidpi(app, widget, out_path: str, size: tuple[int, int], scale: float = 2.0) -> None:
    from PyQt6.QtGui import QPixmap

    widget.resize(*size)
    widget.show()
    widget.raise_()
    _wait_and_flush(app, 200)

    w, h = widget.width(), widget.height()
    pixmap = QPixmap(int(round(w * scale)), int(round(h * scale)))
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(widget.palette().color(widget.backgroundRole()))
    widget.render(pixmap)
    pixmap.save(out_path, "PNG")


def capture_demo_screenshots(
    app,
    tray_app,
    output_dir: str = "docs/images",
    scale: float = 2.0,
) -> list[str]:
    """Navigate through key views, resize cleanly, and save 2x HiDPI window screenshots."""
    import os

    os.makedirs(output_dir, exist_ok=True)
    panel = tray_app.panel
    saved: list[str] = []

    panel.show_window()
    _wait_and_flush(app, 200)

    # 1. Overview
    panel._show_overview()
    overview_path = os.path.join(output_dir, "overview.png")
    _render_widget_hidpi(app, panel, overview_path, (780, 520), scale=scale)
    saved.append(overview_path)

    # 2. Provider detail: shared quota pools, Argon quota, and seven-decimal precision
    panel.select_provider("antigravity")
    detail_path = os.path.join(output_dir, "provider-detail.png")
    _render_widget_hidpi(app, panel, detail_path, (900, 1000), scale=scale)
    saved.append(detail_path)

    # 3. Provider-focused analytics with compact forecasts and collapsed usage patterns
    panel._show_analytics()
    analytics_path = os.path.join(output_dir, "analytics.png")
    _render_widget_hidpi(app, panel, analytics_path, (1100, 1000), scale=scale)
    saved.append(analytics_path)

    panel.hide()
    return saved
