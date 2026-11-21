from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


def to_decimal(value: Any, default: str = "0") -> Decimal:
    """Convert a JSON numeric value or string to Decimal without binary float drift."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def clamp_fraction(value: Decimal) -> Decimal:
    """Clamp a fraction to [0, 1]."""
    if value < Decimal("0"):
        return Decimal("0")
    if value > Decimal("1"):
        return Decimal("1")
    return value


def format_remaining_percent(remaining_fraction: Decimal) -> str:
    """Format remaining fraction as an exact percentage string (e.g. 0.9968221 -> '99.68221%').

    Preserves all significant decimal digits without unnecessary trailing zeros
    for whole numbers (1 -> '100%', 0 -> '0%').
    """
    pct = (clamp_fraction(remaining_fraction) * Decimal("100")).quantize(Decimal("0.0000001"))
    text = format(pct, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}%"


def format_countdown(reset_time: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Format a human-readable countdown to reset_time."""
    if reset_time is None:
        return "Reset time unknown"
    current = now or datetime.now(timezone.utc)
    if reset_time.tzinfo is None:
        reset_time = reset_time.replace(tzinfo=timezone.utc)
    delta = reset_time - current
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "Resetting now"

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return "Resets in " + " ".join(parts)


@dataclass(slots=True)
class QuotaBucket:
    """A single rate-limit or quota window."""

    bucket_id: str
    display_name: str
    window: str
    remaining_fraction: Decimal
    reset_time: Optional[datetime] = None
    description: str = ""

    @property
    def remaining_percent_str(self) -> str:
        return format_remaining_percent(self.remaining_fraction)

    @property
    def remaining_float(self) -> float:
        return float(clamp_fraction(self.remaining_fraction))


@dataclass(slots=True)
class QuotaGroup:
    """A logical group of quota buckets (e.g. 'Gemini Models', 'Codex')."""

    group_id: str
    display_name: str
    description: str = ""
    buckets: list[QuotaBucket] = field(default_factory=list)


@dataclass(slots=True)
class ProviderUsageSnapshot:
    """Normalized usage snapshot returned by any UsageProvider."""

    provider_id: str
    provider_name: str
    source_label: str
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    groups: list[QuotaGroup] = field(default_factory=list)
    plan_name: Optional[str] = None
    badges: list[str] = field(default_factory=list)
    error: Optional[str] = None
    auth_required: bool = False
    rate_limited_until: Optional[datetime] = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def all_buckets(self) -> list[QuotaBucket]:
        return [b for g in self.groups for b in g.buckets]

    @property
    def lowest_remaining_fraction(self) -> Optional[Decimal]:
        buckets = self.all_buckets
        if not buckets:
            return None
        return min(b.remaining_fraction for b in buckets)


@dataclass(slots=True)
class TokenUsageRecord:
    """Forward-compatible record for future local token & cost tracking."""

    timestamp: datetime
    provider_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    cost_usd: Optional[Decimal] = None
