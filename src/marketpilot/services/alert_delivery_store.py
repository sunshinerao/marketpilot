from __future__ import annotations

import json
import sqlite3
from datetime import UTC
from pathlib import Path
from threading import RLock
from typing import Any

from marketpilot.domain.alert_delivery import DeliveryAttempt, OutboxEvent, OutboxMessage


class AlertDeliveryConflict(ValueError):
    """An immutable identity was reused with different content."""


class AlertDeliveryStore:
    """SQLite append-only outbox, event log, and delivery-attempt audit."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def append_message(self, message: OutboxMessage) -> OutboxMessage:
        self._append_immutable(
            table="alert_outbox_messages",
            key_column="message_id",
            key=message.message_id,
            payload=message.model_dump(mode="json"),
            columns={
                "created_at": message.created_at.astimezone(UTC).isoformat(),
                "dedupe_key": message.dedupe_key,
                "channel": message.channel.value,
            },
        )
        stored = self.message(message.message_id)
        if stored is None:  # pragma: no cover - protected by transaction
            raise RuntimeError("outbox insert was not readable")
        return stored

    def append_event(self, event: OutboxEvent) -> OutboxEvent:
        if self.message(event.message_id) is None:
            raise KeyError(f"unknown outbox message: {event.message_id}")
        self._append_immutable(
            table="alert_outbox_events",
            key_column="event_id",
            key=event.event_id,
            payload=event.model_dump(mode="json"),
            columns={
                "message_id": event.message_id,
                "recorded_at": event.recorded_at.astimezone(UTC).isoformat(),
                "kind": event.kind.value,
            },
        )
        return event

    def append_attempt(self, attempt: DeliveryAttempt) -> DeliveryAttempt:
        if self.message(attempt.message_id) is None:
            raise KeyError(f"unknown outbox message: {attempt.message_id}")
        self._append_immutable(
            table="alert_delivery_attempts",
            key_column="attempt_id",
            key=attempt.attempt_id,
            payload=attempt.model_dump(mode="json"),
            columns={
                "message_id": attempt.message_id,
                "attempted_at": attempt.attempted_at.astimezone(UTC).isoformat(),
                "outcome": attempt.outcome.value,
            },
        )
        return attempt

    def message(self, message_id: str) -> OutboxMessage | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM alert_outbox_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return OutboxMessage.model_validate_json(row["payload_json"]) if row else None

    def messages(self) -> tuple[OutboxMessage, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM alert_outbox_messages ORDER BY created_at, message_id"
            ).fetchall()
        return tuple(OutboxMessage.model_validate_json(row["payload_json"]) for row in rows)

    def events(self, message_id: str | None = None) -> tuple[OutboxEvent, ...]:
        with self._lock:
            if message_id is None:
                rows = self._connection.execute(
                    "SELECT payload_json FROM alert_outbox_events "
                    "ORDER BY recorded_at, sequence"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload_json FROM alert_outbox_events WHERE message_id = ? "
                    "ORDER BY recorded_at, sequence",
                    (message_id,),
                ).fetchall()
        return tuple(OutboxEvent.model_validate_json(row["payload_json"]) for row in rows)

    def attempts(self, message_id: str | None = None) -> tuple[DeliveryAttempt, ...]:
        with self._lock:
            if message_id is None:
                rows = self._connection.execute(
                    "SELECT payload_json FROM alert_delivery_attempts "
                    "ORDER BY attempted_at, attempt_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload_json FROM alert_delivery_attempts WHERE message_id = ? "
                    "ORDER BY attempted_at, attempt_id",
                    (message_id,),
                ).fetchall()
        return tuple(DeliveryAttempt.model_validate_json(row["payload_json"]) for row in rows)

    def latest_message_for_dedupe(self, dedupe_key: str) -> OutboxMessage | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM alert_outbox_messages WHERE dedupe_key = ? "
                "ORDER BY created_at DESC, message_id DESC LIMIT 1",
                (dedupe_key,),
            ).fetchone()
        return OutboxMessage.model_validate_json(row["payload_json"]) if row else None

    def _append_immutable(
        self,
        *,
        table: str,
        key_column: str,
        key: str,
        payload: dict[str, Any],
        columns: dict[str, str],
    ) -> None:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connection:
            existing = self._connection.execute(
                f"SELECT payload_json FROM {table} WHERE {key_column} = ?", (key,)
            ).fetchone()
            if existing is not None:
                if str(existing["payload_json"]) != serialized:
                    raise AlertDeliveryConflict(f"immutable audit conflict: {table}/{key}")
                return
            names = [key_column, *columns, "payload_json"]
            values = [key, *columns.values(), serialized]
            placeholders = ", ".join("?" for _ in names)
            self._connection.execute(
                f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})", values
            )

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS alert_outbox_messages (
                    message_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alert_outbox_dedupe_idx
                    ON alert_outbox_messages(dedupe_key, created_at);
                CREATE TABLE IF NOT EXISTS alert_outbox_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    message_id TEXT NOT NULL REFERENCES alert_outbox_messages(message_id),
                    recorded_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alert_outbox_event_message_idx
                    ON alert_outbox_events(message_id, recorded_at);
                CREATE TABLE IF NOT EXISTS alert_delivery_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL REFERENCES alert_outbox_messages(message_id),
                    attempted_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alert_delivery_attempt_message_idx
                    ON alert_delivery_attempts(message_id, attempted_at);
                """
            )
            for table in (
                "alert_outbox_messages",
                "alert_outbox_events",
                "alert_delivery_attempts",
            ):
                self._connection.executescript(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {table}_deny_update
                    BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
                    CREATE TRIGGER IF NOT EXISTS {table}_deny_delete
                    BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
                    """
                )
