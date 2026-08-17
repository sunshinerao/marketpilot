from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from marketpilot.domain.alerts import (
    AlertCandidate,
    AlertPriority,
    AlertRecord,
    AlertStatus,
)
from marketpilot.domain.attribution import (
    AttributionReview,
    AttributionReviewStatus,
    AttributionSignal,
    AttributionTriggerKind,
    CandidateCause,
)
from marketpilot.domain.events import EventSeverity
from marketpilot.domain.streaming import DeliveryAuditRecord
from marketpilot.services.persistence_contracts import StreamAttributionRepository
from marketpilot.services.postgres_stream_attribution_store import (
    PostgreSQLStreamAttributionStore,
)
from marketpilot.services.stream_attribution_service import StreamAttributionService
from marketpilot.services.stream_attribution_store import Phase4AuditConflict, StreamCursorError

NOW = datetime(2026, 8, 17, 13, tzinfo=UTC)


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.rows: list[Any] = []
        self.rowcount = 0

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> FakeCursor:
        sql = " ".join(query.split())
        if "FROM pg_trigger" in sql:
            self.rows = [(8,)]
            return self
        if "COALESCE(MAX(sequence), 0)" in sql:
            maximum = max(
                (int(row["sequence"]) for row in self.connection.rows("alert_stream_events")),
                default=0,
            )
            self.rows = [(maximum,)]
            return self

        insert = re.search(r"INSERT INTO marketpilot\.(\w+)\s*\(([^)]+)\)", sql)
        if insert:
            table = insert.group(1)
            columns = [value.strip() for value in insert.group(2).split(",")]
            row = dict(zip(columns, params, strict=True))
            if table == "alert_stream_events":
                key_column = "projection_key"
                self.connection.sequence += 1
                row["sequence"] = self.connection.sequence
            else:
                key_column = columns[0]
            existing = self.connection.find(table, key_column, row[key_column])
            if existing is not None:
                self.rowcount = 0
                self.rows = []
            else:
                self.connection.rows(table).append(row)
                self.rowcount = 1
                self.rows = (
                    [(row["sequence"], row["payload_json"], row["recorded_at"])]
                    if "RETURNING sequence" in sql
                    else []
                )
            return self

        table_match = re.search(r"FROM marketpilot\.(\w+)", sql)
        if table_match is None:
            raise AssertionError(f"unsupported SQL: {sql}")
        table = table_match.group(1)
        rows = list(self.connection.rows(table))
        if "WHERE projection_key = %s" in sql:
            rows = [row for row in rows if row["projection_key"] == params[0]]
        elif "WHERE sequence > %s" in sql:
            rows = [row for row in rows if int(row["sequence"]) > int(params[0])]
        elif "WHERE stream_event_id = %s" in sql:
            rows = [row for row in rows if int(row["stream_event_id"]) == int(params[0])]
        elif "WHERE signal_id = %s" in sql:
            rows = [row for row in rows if row["signal_id"] == params[0]]
        elif "WHERE task_id = %s" in sql:
            rows = [row for row in rows if row["task_id"] == params[0]]
        elif "WHERE" in sql:
            key_column = {
                "stream_deliveries": "delivery_id",
                "attribution_tasks": "task_id",
                "attribution_reviews": "review_id",
            }[table]
            rows = [row for row in rows if row[key_column] == params[0]]

        if "ORDER BY sequence" in sql:
            rows.sort(key=lambda row: int(row["sequence"]))
        elif "ORDER BY created_at DESC" in sql:
            rows.sort(key=lambda row: str(row["created_at"]), reverse=True)
        elif "ORDER BY reviewed_at" in sql:
            rows.sort(key=lambda row: (str(row["reviewed_at"]), str(row["review_id"])))
        elif "ORDER BY attempted_at" in sql:
            rows.sort(key=lambda row: (str(row["attempted_at"]), str(row["delivery_id"])))

        if "SELECT sequence, payload_json, recorded_at" in sql:
            self.rows = [
                (row["sequence"], row["payload_json"], row["recorded_at"]) for row in rows
            ]
        else:
            self.rows = [(row["payload_json"],) for row in rows]
        return self

    def fetchone(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return self.rows

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.sequence = 0
        self.commits = 0
        self.rollbacks = 0

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self.tables.setdefault(table, [])

    def find(self, table: str, column: str, value: Any) -> dict[str, Any] | None:
        return next((row for row in self.rows(table) if row[column] == value), None)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass


def alert(*, status: AlertStatus = AlertStatus.OPEN) -> AlertRecord:
    return AlertRecord(
        alert_id="alert-1",
        candidate=AlertCandidate(
            fingerprint="tail-risk",
            priority=AlertPriority.P0,
            direction="DOWNSIDE",
            observed_at=NOW,
            evidence=("ES_LIQUIDITY_DROP",),
            event_id="shock-1",
            snapshot_id="sha256:alert",
            model_version="model-v1",
            rules_version="rules-v1",
            action="NO_TRADE",
            rerun_trigger="T+2m",
        ),
        created_at=NOW,
        status=status,
    )


def signal() -> AttributionSignal:
    return AttributionSignal(
        signal_id="signal-major-event",
        kind=AttributionTriggerKind.MAJOR_EVENT,
        severity=EventSeverity.P0,
        observed_as_of=NOW + timedelta(minutes=2),
        first_seen_at=NOW + timedelta(seconds=30),
        market_reaction_start_at=NOW,
        snapshot_id="sha256:trigger",
        replay_manifest_hash="sha256:manifest",
        candidates=(
            CandidateCause(
                cause_id="cause-1",
                summary="Scheduled macro release",
                source_published_at=NOW + timedelta(seconds=20),
                first_seen_at=NOW + timedelta(seconds=25),
                confidence=0.8,
                evidence_refs=("pit:event:1",),
            ),
        ),
    )


def store() -> tuple[PostgreSQLStreamAttributionStore, FakeConnection]:
    connection = FakeConnection()
    return PostgreSQLStreamAttributionStore(lambda: connection), connection


def test_postgres_stream_projection_cursor_delivery_and_conflict_parity() -> None:
    target, connection = store()
    assert isinstance(target, StreamAttributionRepository)

    first = target.append_alert_projection(
        projection_key="projection-1", alert=alert(), recorded_at=NOW
    )
    repeated = target.append_alert_projection(
        projection_key="projection-1", alert=alert(), recorded_at=NOW
    )
    assert first == repeated
    assert first.event_id == "1"
    assert target.stream_events_after(None) == (first,)
    assert target.stream_events_after("1") == ()
    with pytest.raises(StreamCursorError):
        target.validate_cursor("2")
    with pytest.raises(Phase4AuditConflict):
        target.append_alert_projection(
            projection_key="projection-1",
            alert=alert(status=AlertStatus.ACKNOWLEDGED),
            recorded_at=NOW,
        )

    delivery = DeliveryAuditRecord(
        delivery_id="delivery-1",
        connection_id="connection-1",
        stream_event_id="1",
        attempted_at=NOW,
    )
    target.append_delivery(delivery)
    target.append_delivery(delivery)
    assert target.deliveries("1") == (delivery,)
    assert connection.rollbacks == 1  # projection conflict rolled back atomically.
    assert target.verify_append_only_triggers()


def test_postgres_attribution_task_and_review_projection_parity() -> None:
    target, _ = store()
    service = StreamAttributionService(target, lambda: (), clock=lambda: NOW)
    task = service.create_attribution_task(signal())
    assert service.create_attribution_task(signal()) == task

    review = AttributionReview(
        review_id="review-1",
        task_id=task.task_id,
        status=AttributionReviewStatus.CONFIRMED,
        reviewer="operator",
        reviewed_at=NOW + timedelta(minutes=3),
        note="confirmed from PIT evidence",
        retain_as_reusable_sample=True,
    )
    target.append_attribution_review(review)
    target.append_attribution_review(review)

    restored = target.get_attribution_task(task.task_id)
    assert restored is not None
    assert restored.review_status is AttributionReviewStatus.CONFIRMED
    assert restored.retained_as_reusable_sample
    assert target.attribution_reviews(task.task_id) == (review,)
    with pytest.raises(KeyError, match="unknown attribution task"):
        target.append_attribution_review(review.model_copy(update={"task_id": "missing"}))


def test_postgres_stream_failure_injection_prevents_writes() -> None:
    connection = FakeConnection()

    def fail(operation: str) -> None:
        if operation == "append_delivery":
            raise RuntimeError("injected delivery failure")

    target = PostgreSQLStreamAttributionStore(lambda: connection, failure_injector=fail)
    with pytest.raises(RuntimeError, match="injected"):
        target.append_delivery(
            DeliveryAuditRecord(
                delivery_id="delivery-1",
                connection_id="connection-1",
                stream_event_id="1",
                attempted_at=NOW,
            )
        )
    assert connection.rows("stream_deliveries") == []
