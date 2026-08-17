from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from marketpilot.domain.alerts import AlertRecord
from marketpilot.domain.attribution import AttributionReview, AttributionTask
from marketpilot.domain.streaming import AlertStreamEvent, DeliveryAuditRecord

FailureInjector = Callable[[str], None]


class Phase4AuditConflict(ValueError):
    """An immutable identity was reused with different content."""


class StreamCursorError(ValueError):
    """A Last-Event-ID cursor is malformed or ahead of the local stream."""


class StreamAttributionStore:
    """Local append-only audit store dedicated to streaming and attribution."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._failure_injector = failure_injector
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level="DEFERRED",
        )
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def append_alert_projection(
        self,
        *,
        projection_key: str,
        alert: AlertRecord,
        recorded_at: datetime,
    ) -> AlertStreamEvent:
        self._inject("append_stream_event")
        serialized = _serialize(alert.model_dump(mode="json"))
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT sequence, payload_json, recorded_at FROM alert_stream_events "
                "WHERE projection_key = ?",
                (projection_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_json"]) != serialized:
                    raise Phase4AuditConflict(
                        f"immutable stream projection conflict: {projection_key}"
                    )
                return self._stream_event(existing)
            cursor = self._connection.execute(
                "INSERT INTO alert_stream_events(projection_key, recorded_at, payload_json) "
                "VALUES (?, ?, ?)",
                (projection_key, recorded_at.astimezone(UTC).isoformat(), serialized),
            )
            row = self._connection.execute(
                "SELECT sequence, payload_json, recorded_at FROM alert_stream_events "
                "WHERE sequence = ?",
                (cursor.lastrowid,),
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the insert transaction.
            raise RuntimeError("stream event insert was not readable")
        return self._stream_event(row)

    def stream_events_after(self, event_id: str | None) -> tuple[AlertStreamEvent, ...]:
        sequence = self.validate_cursor(event_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT sequence, payload_json, recorded_at FROM alert_stream_events "
                "WHERE sequence > ? ORDER BY sequence",
                (sequence,),
            ).fetchall()
        return tuple(self._stream_event(row) for row in rows)

    def validate_cursor(self, event_id: str | None) -> int:
        if event_id is None or event_id == "":
            return 0
        try:
            sequence = int(event_id)
        except ValueError as exc:
            raise StreamCursorError("Last-Event-ID must be a positive integer") from exc
        if sequence < 0 or str(sequence) != event_id:
            raise StreamCursorError("Last-Event-ID must be a canonical non-negative integer")
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS maximum FROM alert_stream_events"
            ).fetchone()
        maximum = int(row["maximum"]) if row is not None else 0
        if sequence > maximum:
            raise StreamCursorError("Last-Event-ID is ahead of the local stream")
        return sequence

    def append_delivery(self, record: DeliveryAuditRecord) -> None:
        self._inject("append_delivery")
        self._append_immutable(
            table="stream_deliveries",
            key_column="delivery_id",
            key=record.delivery_id,
            timestamp_column="attempted_at",
            timestamp=record.attempted_at.astimezone(UTC).isoformat(),
            payload=record.model_dump(mode="json"),
            extra_columns={"stream_event_id": record.stream_event_id},
        )

    def deliveries(self, stream_event_id: str | None = None) -> tuple[DeliveryAuditRecord, ...]:
        with self._lock:
            if stream_event_id is None:
                rows = self._connection.execute(
                    "SELECT payload_json FROM stream_deliveries ORDER BY attempted_at, delivery_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload_json FROM stream_deliveries WHERE stream_event_id = ? "
                    "ORDER BY attempted_at, delivery_id",
                    (stream_event_id,),
                ).fetchall()
        return tuple(DeliveryAuditRecord.model_validate_json(row["payload_json"]) for row in rows)

    def append_attribution_task(self, task: AttributionTask) -> AttributionTask:
        self._inject("append_attribution_task")
        self._append_immutable(
            table="attribution_tasks",
            key_column="task_id",
            key=task.task_id,
            timestamp_column="created_at",
            timestamp=task.created_at.astimezone(UTC).isoformat(),
            payload=task.model_dump(mode="json"),
            extra_columns={"signal_id": task.signal.signal_id},
        )
        stored = self.task_by_signal(task.signal.signal_id)
        if stored is None:  # pragma: no cover - protected by the append transaction.
            raise RuntimeError("attribution task insert was not readable")
        return stored

    def task_by_signal(self, signal_id: str) -> AttributionTask | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM attribution_tasks WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
        return AttributionTask.model_validate_json(row["payload_json"]) if row else None

    def get_attribution_task(self, task_id: str) -> AttributionTask | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM attribution_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return self._with_review_state(AttributionTask.model_validate_json(row["payload_json"]))

    def attribution_tasks(self) -> tuple[AttributionTask, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM attribution_tasks ORDER BY created_at DESC, task_id"
            ).fetchall()
        return tuple(
            self._with_review_state(AttributionTask.model_validate_json(row["payload_json"]))
            for row in rows
        )

    def append_attribution_review(self, review: AttributionReview) -> None:
        self._inject("append_attribution_review")
        if self.get_attribution_task(review.task_id) is None:
            raise KeyError(f"unknown attribution task: {review.task_id}")
        self._append_immutable(
            table="attribution_reviews",
            key_column="review_id",
            key=review.review_id,
            timestamp_column="reviewed_at",
            timestamp=review.reviewed_at.astimezone(UTC).isoformat(),
            payload=review.model_dump(mode="json"),
            extra_columns={"task_id": review.task_id},
        )

    def attribution_reviews(self, task_id: str) -> tuple[AttributionReview, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM attribution_reviews WHERE task_id = ? "
                "ORDER BY reviewed_at, review_id",
                (task_id,),
            ).fetchall()
        return tuple(AttributionReview.model_validate_json(row["payload_json"]) for row in rows)

    def _with_review_state(self, task: AttributionTask) -> AttributionTask:
        reviews = self.attribution_reviews(task.task_id)
        if not reviews:
            return task
        latest = reviews[-1]
        retained = task.retained_as_reusable_sample or any(
            review.retain_as_reusable_sample for review in reviews
        )
        return task.model_copy(
            update={
                "review_status": latest.status,
                "retained_as_reusable_sample": retained,
            }
        )

    def _append_immutable(
        self,
        *,
        table: str,
        key_column: str,
        key: str,
        timestamp_column: str,
        timestamp: str,
        payload: dict[str, Any],
        extra_columns: dict[str, str],
    ) -> None:
        serialized = _serialize(payload)
        with self._lock, self._connection:
            existing = self._connection.execute(
                f"SELECT payload_json FROM {table} WHERE {key_column} = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_json"]) != serialized:
                    raise Phase4AuditConflict(f"immutable audit conflict: {table}/{key}")
                return
            columns = [key_column, timestamp_column, "payload_json", *extra_columns]
            values = [key, timestamp, serialized, *extra_columns.values()]
            placeholders = ", ".join("?" for _ in columns)
            self._connection.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS alert_stream_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    projection_key TEXT NOT NULL UNIQUE,
                    recorded_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stream_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    stream_event_id INTEGER NOT NULL REFERENCES alert_stream_events(sequence),
                    attempted_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS stream_delivery_event_idx
                    ON stream_deliveries(stream_event_id, attempted_at);
                CREATE TABLE IF NOT EXISTS attribution_tasks (
                    task_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attribution_reviews (
                    review_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES attribution_tasks(task_id),
                    reviewed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attribution_review_task_idx
                    ON attribution_reviews(task_id, reviewed_at);
                COMMIT;
                """
            )
            for table in (
                "alert_stream_events",
                "stream_deliveries",
                "attribution_tasks",
                "attribution_reviews",
            ):
                self._connection.executescript(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {table}_deny_update
                    BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'append-only phase4 audit table'); END;
                    CREATE TRIGGER IF NOT EXISTS {table}_deny_delete
                    BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'append-only phase4 audit table'); END;
                    """
                )

    def _inject(self, operation: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(operation)

    @staticmethod
    def _stream_event(row: sqlite3.Row) -> AlertStreamEvent:
        return AlertStreamEvent(
            event_id=str(row["sequence"]),
            recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
            alert=AlertRecord.model_validate_json(row["payload_json"]),
        )


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
