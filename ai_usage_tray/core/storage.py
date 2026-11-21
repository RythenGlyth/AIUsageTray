from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from ai_usage_tray.core.config import default_data_dir
from ai_usage_tray.core.models import ProviderUsageSnapshot, TokenUsageRecord


class HistoryStorage:
    """SQLite persistence for quota history (sparklines) and future token tracking."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            data_dir = default_data_dir()
            os.makedirs(data_dir, mode=0o700, exist_ok=True)
            db_path = os.path.join(data_dir, "history.db")
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fetched_at TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    bucket_id TEXT NOT NULL,
                    remaining_fraction TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_quota_provider_bucket
                ON quota_snapshots (provider_id, bucket_id, id DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd TEXT
                )
                """
            )
            conn.commit()

    def record_snapshot(self, snapshot: ProviderUsageSnapshot) -> None:
        if snapshot.error or not snapshot.all_buckets:
            return
        ts = snapshot.fetched_at.astimezone(timezone.utc).isoformat()
        rows = [
            (ts, snapshot.provider_id, b.bucket_id, str(b.remaining_fraction))
            for b in snapshot.all_buckets
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO quota_snapshots (fetched_at, provider_id, bucket_id, remaining_fraction) VALUES (?, ?, ?, ?)",
                rows,
            )
            conn.commit()

    def get_bucket_history_points(
        self,
        provider_id: str,
        bucket_id: str,
        limit: int = 24,
    ) -> list[tuple[datetime, float]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT fetched_at, remaining_fraction FROM quota_snapshots
                WHERE provider_id = ? AND bucket_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (provider_id, bucket_id, limit),
            ).fetchall()
        points: list[tuple[datetime, float]] = []
        for ts_str, frac_str in reversed(rows):
            try:
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                points.append((dt, float(Decimal(frac_str))))
            except Exception:
                continue
        return points

    def get_bucket_history(self, provider_id: str, bucket_id: str, limit: int = 24) -> list[float]:
        return [val for _, val in self.get_bucket_history_points(provider_id, bucket_id, limit=limit)]

    def get_bucket_history_since(
        self,
        provider_id: str,
        bucket_id: str,
        since: datetime,
    ) -> list[tuple[datetime, float]]:
        since_iso = since.astimezone(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT fetched_at, remaining_fraction FROM quota_snapshots
                WHERE provider_id = ? AND bucket_id = ? AND fetched_at >= ?
                ORDER BY id ASC
                """,
                (provider_id, bucket_id, since_iso),
            ).fetchall()
        points: list[tuple[datetime, float]] = []
        for ts_str, frac_str in rows:
            try:
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                points.append((dt, float(Decimal(frac_str))))
            except Exception:
                continue
        return points

    def get_all_series_since(self, since: datetime) -> dict[tuple[str, str], list[tuple[datetime, float]]]:
        """Return {(provider_id, bucket_id): [(dt, remaining_float), ...]} since the given UTC datetime."""
        since_iso = since.astimezone(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT fetched_at, provider_id, bucket_id, remaining_fraction
                FROM quota_snapshots
                WHERE fetched_at >= ?
                ORDER BY id ASC
                """,
                (since_iso,),
            ).fetchall()
        series: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
        for ts_str, pid, bid, frac_str in rows:
            try:
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                series.setdefault((str(pid), str(bid)), []).append((dt, float(Decimal(frac_str))))
            except Exception:
                continue
        return series

    def get_token_usage_summary(self, since: datetime) -> list[tuple[str, str, int, int, int]]:
        """Return [(provider_id, model, input_tokens, output_tokens, cached_tokens), ...] since datetime."""
        since_iso = since.astimezone(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT provider_id, model,
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(cached_tokens), 0)
                FROM token_usage
                WHERE timestamp >= ?
                GROUP BY provider_id, model
                """,
                (since_iso,),
            ).fetchall()
        return [(str(r[0]), str(r[1]), int(r[2]), int(r[3]), int(r[4])) for r in rows]

    def record_token_usage(self, record: TokenUsageRecord) -> None:
        """Store a token usage event (extensibility hook for future token tracking)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO token_usage (timestamp, provider_id, model, input_tokens, output_tokens, cached_tokens, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.timestamp.astimezone(timezone.utc).isoformat(),
                    record.provider_id,
                    record.model,
                    record.input_tokens,
                    record.output_tokens,
                    record.cached_tokens,
                    str(record.cost_usd) if record.cost_usd is not None else None,
                ),
            )
            conn.commit()
