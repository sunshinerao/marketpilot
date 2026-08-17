from __future__ import annotations

import json
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from marketpilot.domain.alerts import AlertRecord
from marketpilot.domain.attribution import AttributionReview, AttributionTask
from marketpilot.domain.streaming import AlertStreamEvent, DeliveryAuditRecord
from marketpilot.services.persistence_contracts import ConnectionFactory
from marketpilot.services.stream_attribution_store import (
    FailureInjector,
    Phase4AuditConflict,
    StreamCursorError,
)


class PostgreSQLStreamAttributionStore:
    """PostgreSQL parity adapter for resumable alert streaming and attribution."""

    _IMMUTABLE_TABLES = (
        "alert_stream_events",
        "stream_deliveries",
        "attribution_tasks",
        "attribution_reviews",
    )

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        *,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self._connection = connection_factory()
        self._failure_injector = failure_injector
        self._lock = RLock()

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
        timestamp = self._utc(recorded_at, "recorded_at").isoformat()
        serialized = self._serialize(alert.model_dump(mode="json"))
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO marketpilot.alert_stream_events(
                        projection_key, recorded_at, payload_json
                    ) VALUES (%s, %s, %s)
                    ON CONFLICT (projection_key) DO NOTHING
                    RETURNING sequence, payload_json, recorded_at
                    """,
                    (projection_key, timestamp, serialized),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        SELECT sequence, payload_json, recorded_at
                        FROM marketpilot.alert_stream_events
                        WHERE projection_key = %s
                        FOR SHARE
                        """,
                        (projection_key,),
                    )
                    row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("stream event insert was not readable")
                stored = self._serialize(self._cell(row, "payload_json", 1))
                if stored != serialized:
                    raise Phase4AuditConflict(
                        f"immutable stream projection conflict: {projection_key}"
                    )
                event = self._stream_event(row)
                self._connection.commit()
                return event
            except Exception:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    def stream_events_after(self, event_id: str | None) -> tuple[AlertStreamEvent, ...]:
        sequence = self.validate_cursor(event_id)
        rows = self._read_all(
            """
            SELECT sequence, payload_json, recorded_at
            FROM marketpilot.alert_stream_events
            WHERE sequence > %s
            ORDER BY sequence
            """,
            (sequence,),
        )
        return tuple(self._stream_event(row) for row in rows)

    def validate_cursor(self, event_id: str | None) -> int:
        if event_id is None or event_id == "":
            sequence = 0
        else:
            try:
                sequence = int(event_id)
            except ValueError as error:
                raise StreamCursorError("Last-Event-ID must be a positive integer") from error
            if sequence < 0 or str(sequence) != event_id:
                raise StreamCursorError(
                    "Last-Event-ID must be a canonical non-negative integer"
                )
        row = self._read_one(
            "SELECT COALESCE(MAX(sequence), 0) FROM marketpilot.alert_stream_events"
        )
        maximum = int(self._cell(row, "maximum", 0)) if row is not None else 0
        if sequence > maximum:
            raise StreamCursorError("Last-Event-ID is ahead of the local stream")
        return sequence

    def append_delivery(self, record: DeliveryAuditRecord) -> None:
        self._inject("append_delivery")
        stream_sequence = self._canonical_sequence(record.stream_event_id)
        self._append_immutable(
            table="stream_deliveries",
            key_column="delivery_id",
            key=record.delivery_id,
            timestamp_column="attempted_at",
            timestamp=self._utc(record.attempted_at, "attempted_at").isoformat(),
            payload=record.model_dump(mode="json"),
            extra_columns={"stream_event_id": stream_sequence},
        )

    def deliveries(self, stream_event_id: str | None = None) -> tuple[DeliveryAuditRecord, ...]:
        query = "SELECT payload_json FROM marketpilot.stream_deliveries"
        params: tuple[Any, ...] = ()
        if stream_event_id is not None:
            query += " WHERE stream_event_id = %s"
            params = (self._canonical_sequence(stream_event_id),)
        query += " ORDER BY attempted_at, delivery_id"
        return tuple(
            DeliveryAuditRecord.model_validate(self._object(self._cell(row, "payload_json", 0)))
            for row in self._read_all(query, params)
        )

    def append_attribution_task(self, task: AttributionTask) -> AttributionTask:
        self._inject("append_attribution_task")
        self._append_immutable(
            table="attribution_tasks",
            key_column="task_id",
            key=task.task_id,
            timestamp_column="created_at",
            timestamp=self._utc(task.created_at, "created_at").isoformat(),
            payload=task.model_dump(mode="json"),
            extra_columns={"signal_id": task.signal.signal_id},
        )
        stored = self.task_by_signal(task.signal.signal_id)
        if stored is None:
            raise RuntimeError("attribution task insert was not readable")
        return stored

    def task_by_signal(self, signal_id: str) -> AttributionTask | None:
        row = self._read_one(
            "SELECT payload_json FROM marketpilot.attribution_tasks WHERE signal_id = %s",
            (signal_id,),
        )
        return None if row is None else self._task(row)

    def get_attribution_task(self, task_id: str) -> AttributionTask | None:
        row = self._read_one(
            "SELECT payload_json FROM marketpilot.attribution_tasks WHERE task_id = %s",
            (task_id,),
        )
        return None if row is None else self._with_review_state(self._task(row))

    def attribution_tasks(self) -> tuple[AttributionTask, ...]:
        rows = self._read_all(
            """
            SELECT payload_json FROM marketpilot.attribution_tasks
            ORDER BY created_at DESC, task_id
            """
        )
        return tuple(self._with_review_state(self._task(row)) for row in rows)

    def append_attribution_review(self, review: AttributionReview) -> None:
        self._inject("append_attribution_review")
        if self.get_attribution_task(review.task_id) is None:
            raise KeyError(f"unknown attribution task: {review.task_id}")
        self._append_immutable(
            table="attribution_reviews",
            key_column="review_id",
            key=review.review_id,
            timestamp_column="reviewed_at",
            timestamp=self._utc(review.reviewed_at, "reviewed_at").isoformat(),
            payload=review.model_dump(mode="json"),
            extra_columns={"task_id": review.task_id},
        )

    def attribution_reviews(self, task_id: str) -> tuple[AttributionReview, ...]:
        rows = self._read_all(
            """
            SELECT payload_json FROM marketpilot.attribution_reviews
            WHERE task_id = %s ORDER BY reviewed_at, review_id
            """,
            (task_id,),
        )
        return tuple(
            AttributionReview.model_validate(self._object(self._cell(row, "payload_json", 0)))
            for row in rows
        )

    def verify_append_only_triggers(self) -> bool:
        row = self._read_one(
            """
            SELECT count(*)
            FROM pg_trigger t
            JOIN pg_class r ON r.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = r.relnamespace
            JOIN pg_proc p ON p.oid = t.tgfoid
            WHERE n.nspname = %s
              AND r.relname = ANY(%s)
              AND p.proname = %s
              AND NOT t.tgisinternal
              AND t.tgenabled <> 'D'
            """,
            ("marketpilot", list(self._IMMUTABLE_TABLES), "deny_audit_mutation"),
        )
        return row is not None and int(self._cell(row, "count", 0)) == 8

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
        extra_columns: dict[str, Any],
    ) -> None:
        if table not in self._IMMUTABLE_TABLES:
            raise ValueError("unsupported immutable stream table")
        serialized = self._serialize(payload)
        columns = [key_column, timestamp_column, "payload_json", *extra_columns]
        values = [key, timestamp, serialized, *extra_columns.values()]
        placeholders = ", ".join("%s" for _ in values)
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(
                    f"INSERT INTO marketpilot.{table} ({', '.join(columns)}) "
                    f"VALUES ({placeholders}) ON CONFLICT ({key_column}) DO NOTHING",
                    tuple(values),
                )
                if cursor.rowcount != 1:
                    cursor.execute(
                        f"SELECT payload_json FROM marketpilot.{table} "
                        f"WHERE {key_column} = %s FOR SHARE",
                        (key,),
                    )
                    row = cursor.fetchone()
                    existing = (
                        None
                        if row is None
                        else self._serialize(self._cell(row, "payload_json", 0))
                    )
                    if existing != serialized:
                        raise Phase4AuditConflict(f"immutable audit conflict: {table}/{key}")
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    def _read_one(self, query: str, params: tuple[Any, ...] = ()) -> Any | None:
        rows = self._read_all(query, params)
        return rows[0] if rows else None

    def _read_all(self, query: str, params: tuple[Any, ...] = ()) -> list[Any]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(query, params)
                rows = cursor.fetchall()
                self._connection.commit()
                return rows
            except Exception:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    def _task(self, row: Any) -> AttributionTask:
        return AttributionTask.model_validate(self._object(self._cell(row, "payload_json", 0)))

    def _stream_event(self, row: Any) -> AlertStreamEvent:
        return AlertStreamEvent(
            event_id=str(self._cell(row, "sequence", 0)),
            recorded_at=self._datetime(self._cell(row, "recorded_at", 2)),
            alert=AlertRecord.model_validate(self._object(self._cell(row, "payload_json", 1))),
        )

    def _inject(self, operation: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(operation)

    @staticmethod
    def _canonical_sequence(value: str) -> int:
        try:
            sequence = int(value)
        except ValueError as error:
            raise StreamCursorError("stream event id must be a positive integer") from error
        if sequence <= 0 or str(sequence) != value:
            raise StreamCursorError("stream event id must be a canonical positive integer")
        return sequence

    @staticmethod
    def _utc(value: datetime, name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    @staticmethod
    def _serialize(value: Any) -> str:
        if isinstance(value, str):
            value = json.loads(value)
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _object(value: Any) -> dict[str, Any]:
        decoded = json.loads(value) if isinstance(value, str) else value
        if not isinstance(decoded, dict):
            raise TypeError("stream payload must be a JSON object")
        return decoded

    @staticmethod
    def _cell(row: Any, name: str, index: int) -> Any:
        if isinstance(row, dict):
            return row[name]
        try:
            return row[name]
        except (TypeError, KeyError):
            return row[index]
