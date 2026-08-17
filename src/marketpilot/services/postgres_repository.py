from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import import_module
from threading import RLock
from typing import Any, cast

from marketpilot.domain.alerts import AlertFeedback, AlertRecord
from marketpilot.domain.point_in_time import PointInTimeRecord, ReplayManifest
from marketpilot.services.persistence_contracts import (
    Connection,
    ConnectionFactory,
    RecoveryCheckpoint,
)
from marketpilot.services.raw_landing import LandingReceipt
from marketpilot.services.repository import (
    SCHEMA_VERSION,
    AuditIntegrityReport,
    ImmutableAuditConflict,
    PointInTimeRecordMetadata,
    ReplayManifestMetadata,
)
from marketpilot.services.schemas import DecisionRunOutput


class PostgreSQLAuditRepository:
    """Synchronous PostgreSQL adapter for append-only audit metadata.

    Schema installation is deliberately a separate deployment concern; construct this
    adapter only after applying ``migrations/postgresql/0001_audit.sql``. The adapter
    never accepts or stores canonical licensed provider payloads.
    """

    _TABLES = {
        "decision_runs": "run_id",
        "alerts": "alert_id",
        "alert_feedback": "feedback_id",
        "point_in_time_records": "record_id",
        "replay_manifests": "manifest_hash",
        "recovery_checkpoints": "checkpoint_id",
        "raw_landing_receipts": "landing_id",
    }
    _AUDITED_TABLES = (
        *_TABLES,
        "alert_stream_events",
        "stream_deliveries",
        "attribution_tasks",
        "attribution_reviews",
        "governance_model_versions",
        "governance_approvals",
        "governance_events",
        "governance_session_freezes",
    )

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection = connection_factory()
        self._lock = RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def append_decision(self, decision: DecisionRunOutput) -> None:
        self._append_json("decision_runs", decision.run_id, decision.model_dump(mode="json"))

    def get_decision(self, run_id: str) -> DecisionRunOutput | None:
        payload = self._get_json("decision_runs", run_id)
        return DecisionRunOutput.model_validate(payload) if payload is not None else None

    def decisions(self, *, limit: int = 100) -> tuple[DecisionRunOutput, ...]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be in [1, 1000]")
        return tuple(
            DecisionRunOutput.model_validate(payload)
            for payload in self._all_json(
                "decision_runs",
                "recorded_at DESC, run_id DESC",
                limit=limit,
            )
        )

    def append_alert(self, alert: AlertRecord) -> None:
        self._append_json("alerts", alert.alert_id, alert.model_dump(mode="json"))

    def alerts(self) -> tuple[AlertRecord, ...]:
        payloads = self._all_json("alerts", "created_at DESC, alert_id")
        return tuple(AlertRecord.model_validate(payload) for payload in payloads)

    def get_alert(self, alert_id: str) -> AlertRecord | None:
        payload = self._get_json("alerts", alert_id)
        return AlertRecord.model_validate(payload) if payload is not None else None

    def append_feedback(self, feedback: AlertFeedback) -> None:
        if self.get_alert(feedback.alert_id) is None:
            raise KeyError(f"unknown alert: {feedback.alert_id}")
        self._append_json(
            "alert_feedback",
            feedback.feedback_id,
            feedback.model_dump(mode="json"),
            extra={"alert_id": feedback.alert_id},
        )

    def feedback(self, alert_id: str | None = None) -> tuple[AlertFeedback, ...]:
        if alert_id is None:
            payloads = self._all_json("alert_feedback", "recorded_at, feedback_id")
        else:
            payloads = self._all_json(
                "alert_feedback",
                "recorded_at, feedback_id",
                where=("alert_id", alert_id),
            )
        return tuple(AlertFeedback.model_validate(payload) for payload in payloads)

    def append_point_in_time_record(self, record: PointInTimeRecord) -> None:
        record.verify()
        metadata = PointInTimeRecordMetadata(
            record_id=record.record_id,
            logical_key=record.logical_key,
            published_at=record.published_at.isoformat(),
            first_seen_at=record.first_seen_at.isoformat(),
            provider=record.provider,
            provider_version=record.provider_version,
            schema_version=record.schema_version,
            content_hash=record.content_hash,
        )
        self._append_json("point_in_time_records", record.record_id, asdict(metadata))

    def point_in_time_records(self) -> tuple[PointInTimeRecordMetadata, ...]:
        return tuple(
            PointInTimeRecordMetadata(**payload)
            for payload in self._all_json(
                "point_in_time_records", "first_seen_at, record_id"
            )
        )

    def append_replay_manifest(self, manifest: ReplayManifest) -> None:
        manifest.verify_hash()
        metadata = ReplayManifestMetadata(
            manifest_hash=manifest.manifest_hash,
            as_of=manifest.as_of.isoformat(),
            entries=tuple(asdict(entry) for entry in manifest.entries),
        )
        self._append_json("replay_manifests", manifest.manifest_hash, asdict(metadata))

    def replay_manifests(self) -> tuple[ReplayManifestMetadata, ...]:
        return tuple(
            ReplayManifestMetadata(
                manifest_hash=str(payload["manifest_hash"]),
                as_of=str(payload["as_of"]),
                entries=tuple(payload["entries"]),
            )
            for payload in self._all_json("replay_manifests", "as_of, manifest_hash")
        )

    def append_recovery_checkpoint(self, checkpoint: RecoveryCheckpoint) -> None:
        if checkpoint.captured_at.tzinfo is None or checkpoint.captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        required = (
            checkpoint.checkpoint_id,
            checkpoint.database_lsn,
            checkpoint.backup_reference,
            checkpoint.manifest_hash,
            checkpoint.code_version,
            checkpoint.schema_version,
        )
        if any(not value.strip() for value in required):
            raise ValueError("recovery checkpoint fields must not be blank")
        payload = asdict(checkpoint)
        payload["captured_at"] = checkpoint.captured_at.astimezone(UTC).isoformat()
        self._append_json("recovery_checkpoints", checkpoint.checkpoint_id, payload)

    def latest_recovery_checkpoint(self) -> RecoveryCheckpoint | None:
        payloads = self._all_json(
            "recovery_checkpoints", "captured_at DESC, checkpoint_id DESC", limit=1
        )
        if not payloads:
            return None
        payload = payloads[0]
        return RecoveryCheckpoint(
            checkpoint_id=str(payload["checkpoint_id"]),
            captured_at=self._datetime(str(payload["captured_at"])),
            database_lsn=str(payload["database_lsn"]),
            backup_reference=str(payload["backup_reference"]),
            manifest_hash=str(payload["manifest_hash"]),
            code_version=str(payload["code_version"]),
            schema_version=str(payload["schema_version"]),
        )

    def append(self, receipt: LandingReceipt) -> None:
        """Persist a safe landing receipt; satisfies ``LandingMetadataSink``."""

        payload = asdict(receipt)
        payload["published_at"] = receipt.published_at.isoformat()
        payload["first_seen_at"] = receipt.first_seen_at.isoformat()
        forbidden = {
            "payload",
            "plaintext",
            "ciphertext",
            "nonce",
            "canonical_content",
            "wrapped_key",
        }
        if forbidden.intersection(payload):
            raise ValueError("raw landing receipt contains a forbidden payload field")
        self._append_json("raw_landing_receipts", receipt.landing_id, payload)

    def schema_version(self) -> str:
        row = self._execute_one(
            "SELECT value FROM marketpilot.audit_schema WHERE key = %s", ("schema_version",)
        )
        if row is None:
            raise RuntimeError("audit schema version is missing")
        return str(self._cell(row, "value", 0))

    def verify_schema(self) -> None:
        version = self.schema_version()
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported audit schema version: {version}; expected {SCHEMA_VERSION}"
            )

    def integrity_check(self) -> AuditIntegrityReport:
        self.verify_schema()
        # Integrity is a runtime-wide claim, so it must cover every audited table
        # installed by the shared migration, not only tables written by this adapter.
        for table in self._AUDITED_TABLES:
            self._execute_one(f"SELECT 1 FROM marketpilot.{table} LIMIT 1")
        trigger_row = self._execute_one(
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
            ("marketpilot", list(self._AUDITED_TABLES), "deny_audit_mutation"),
        )
        installed_triggers = int(self._cell(trigger_row, "count", 0)) if trigger_row else 0
        expected_triggers = len(self._AUDITED_TABLES) * 2
        constraint_row = self._execute_one(
            """
            SELECT count(*)
            FROM pg_constraint c
            JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname = %s
              AND c.contype = 'f'
              AND NOT c.convalidated
            """,
            ("marketpilot",),
        )
        unvalidated_constraints = (
            int(self._cell(constraint_row, "count", 0)) if constraint_row else 0
        )
        quick_check = (
            ("ok",)
            if installed_triggers == expected_triggers
            else (f"append_only_triggers:{installed_triggers}/{expected_triggers}",)
        )
        return AuditIntegrityReport(
            schema_version=SCHEMA_VERSION,
            quick_check=quick_check,
            foreign_key_violations=unvalidated_constraints,
            append_only_triggers_installed=installed_triggers,
            append_only_triggers_expected=expected_triggers,
        )

    def _append_json(
        self,
        table: str,
        key: str,
        payload: dict[str, Any],
        *,
        extra: dict[str, str] | None = None,
    ) -> None:
        key_column = self._TABLES[table]
        serialized = self._serialize(payload)
        timestamp_column, timestamp = self._timestamp_for(table, payload)
        extra = extra or {}
        columns = [key_column, timestamp_column, "payload_json", *extra]
        values: list[Any] = [key, timestamp, serialized, *extra.values()]
        placeholders = ", ".join("%s" for _ in values)
        query = (
            f"INSERT INTO marketpilot.{table} ({', '.join(columns)}) "
            f"VALUES ({placeholders}) ON CONFLICT ({key_column}) DO NOTHING"
        )
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(query, tuple(values))
                inserted = cursor.rowcount == 1
                if not inserted:
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
                        raise ImmutableAuditConflict(
                            f"immutable audit conflict: {table}/{key}"
                        )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    def _get_json(self, table: str, key: str) -> dict[str, Any] | None:
        key_column = self._TABLES[table]
        row = self._execute_one(
            f"SELECT payload_json FROM marketpilot.{table} WHERE {key_column} = %s", (key,)
        )
        return None if row is None else self._object(self._cell(row, "payload_json", 0))

    def _all_json(
        self,
        table: str,
        order_by: str,
        *,
        where: tuple[str, str] | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        query = f"SELECT payload_json FROM marketpilot.{table}"
        params: tuple[Any, ...] = ()
        if where is not None:
            column, value = where
            if column != "alert_id":
                raise ValueError("unsupported filter")
            query += f" WHERE {column} = %s"
            params = (value,)
        query += f" ORDER BY {order_by}"
        if limit is not None:
            query += " LIMIT %s"
            params += (limit,)
        rows = self._execute_all(query, params)
        return tuple(self._object(self._cell(row, "payload_json", 0)) for row in rows)

    def _execute_one(self, query: str, params: tuple[Any, ...] = ()) -> Any | None:
        rows = self._execute_all(query, params)
        return rows[0] if rows else None

    def _execute_all(self, query: str, params: tuple[Any, ...] = ()) -> list[Any]:
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

    @staticmethod
    def _timestamp_for(table: str, payload: dict[str, Any]) -> tuple[str, str]:
        mapping = {
            "alerts": ("created_at", "created_at"),
            "alert_feedback": ("recorded_at", "recorded_at"),
            "point_in_time_records": ("first_seen_at", "first_seen_at"),
            "replay_manifests": ("as_of", "as_of"),
            "recovery_checkpoints": ("captured_at", "captured_at"),
            "raw_landing_receipts": ("first_seen_at", "first_seen_at"),
            "decision_runs": ("recorded_at", "data_as_of"),
        }
        column, field = mapping[table]
        return column, str(payload[field])

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
            raise TypeError("audit payload must be a JSON object")
        return decoded

    @staticmethod
    def _cell(row: Any, name: str, index: int) -> Any:
        if isinstance(row, dict):
            return row[name]
        try:
            return row[name]
        except (TypeError, KeyError):
            return row[index]

    @staticmethod
    def _datetime(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))


def psycopg_connection_factory(dsn: str) -> ConnectionFactory:
    """Build a lazy psycopg connection factory without importing the optional driver."""

    if not dsn.strip():
        raise ValueError("PostgreSQL DSN must not be blank")

    def connect() -> Connection:
        try:
            psycopg = import_module("psycopg")
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "PostgreSQL support is not installed; install marketpilot[postgres]"
            ) from error
        return cast(Connection, psycopg.connect(dsn))

    return connect
