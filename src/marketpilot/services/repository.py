from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from marketpilot.domain.alerts import AlertFeedback, AlertRecord
from marketpilot.domain.point_in_time import PointInTimeRecord, ReplayManifest
from marketpilot.services.schemas import DecisionRunOutput

SCHEMA_VERSION = "1"


class ImmutableAuditConflict(ValueError):
    """Raised when an existing audit identity is reused with different content."""


@dataclass(frozen=True, slots=True)
class PointInTimeRecordMetadata:
    record_id: str
    logical_key: str
    published_at: str
    first_seen_at: str
    provider: str
    provider_version: str
    schema_version: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class ReplayManifestMetadata:
    manifest_hash: str
    as_of: str
    entries: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class AuditIntegrityReport:
    schema_version: str
    quick_check: tuple[str, ...]
    foreign_key_violations: int
    append_only_triggers_installed: int
    append_only_triggers_expected: int

    @property
    def ok(self) -> bool:
        return (
            self.quick_check == ("ok",)
            and self.foreign_key_violations == 0
            and self.append_only_triggers_installed == self.append_only_triggers_expected
        )


class SQLiteAuditRepository:
    """Append-only SQLite audit store containing metadata and derived outputs only.

    Point-in-time canonical content is intentionally excluded so raw licensed provider
    payloads cannot enter this operational database.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        initialize: bool = True,
        read_only: bool = False,
        require_stream_schema: bool = False,
    ) -> None:
        self.path = str(path)
        if read_only and initialize:
            raise ValueError("read-only repositories cannot initialize schema")
        if read_only and self.path == ":memory:":
            raise ValueError("in-memory repositories cannot be opened read-only")
        if self.path != ":memory:" and not read_only:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._require_stream_schema = require_stream_schema
        connection_path = (
            f"{Path(self.path).resolve().as_uri()}?mode=ro" if read_only else self.path
        )
        self._connection = sqlite3.connect(
            connection_path,
            check_same_thread=False,
            isolation_level="DEFERRED",
            uri=read_only,
        )
        self._connection.row_factory = sqlite3.Row
        if initialize:
            self._initialize()
        else:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def append_decision(self, decision: DecisionRunOutput) -> None:
        self._append_json(
            table="decision_runs",
            key_column="run_id",
            key=decision.run_id,
            payload=decision.model_dump(mode="json"),
        )

    def get_decision(self, run_id: str) -> DecisionRunOutput | None:
        payload = self._get_json("decision_runs", "run_id", run_id)
        return DecisionRunOutput.model_validate(payload) if payload is not None else None

    def decisions(self, *, limit: int = 100) -> tuple[DecisionRunOutput, ...]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be in [1, 1000]")
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM decision_runs "
                "ORDER BY recorded_at DESC, run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(DecisionRunOutput.model_validate_json(row["payload_json"]) for row in rows)

    def append_alert(self, alert: AlertRecord) -> None:
        self._append_json(
            table="alerts",
            key_column="alert_id",
            key=alert.alert_id,
            payload=alert.model_dump(mode="json"),
        )

    def alerts(self) -> tuple[AlertRecord, ...]:
        rows = self._all_json("alerts", "payload_json", "created_at DESC, alert_id")
        return tuple(AlertRecord.model_validate(payload) for payload in rows)

    def get_alert(self, alert_id: str) -> AlertRecord | None:
        payload = self._get_json("alerts", "alert_id", alert_id)
        return AlertRecord.model_validate(payload) if payload is not None else None

    def append_feedback(self, feedback: AlertFeedback) -> None:
        if self.get_alert(feedback.alert_id) is None:
            raise KeyError(f"unknown alert: {feedback.alert_id}")
        self._append_json(
            table="alert_feedback",
            key_column="feedback_id",
            key=feedback.feedback_id,
            payload=feedback.model_dump(mode="json"),
            extra_columns={"alert_id": feedback.alert_id},
        )

    def feedback(self, alert_id: str | None = None) -> tuple[AlertFeedback, ...]:
        with self._lock:
            if alert_id is None:
                rows = self._connection.execute(
                    "SELECT payload_json FROM alert_feedback ORDER BY recorded_at, feedback_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload_json FROM alert_feedback WHERE alert_id = ? "
                    "ORDER BY recorded_at, feedback_id",
                    (alert_id,),
                ).fetchall()
        return tuple(AlertFeedback.model_validate_json(row["payload_json"]) for row in rows)

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
        self._append_json(
            table="point_in_time_records",
            key_column="record_id",
            key=record.record_id,
            payload=asdict(metadata),
        )

    def point_in_time_records(self) -> tuple[PointInTimeRecordMetadata, ...]:
        rows = self._all_json(
            "point_in_time_records",
            "payload_json",
            "first_seen_at, record_id",
        )
        return tuple(PointInTimeRecordMetadata(**payload) for payload in rows)

    def append_replay_manifest(self, manifest: ReplayManifest) -> None:
        manifest.verify_hash()
        metadata = ReplayManifestMetadata(
            manifest_hash=manifest.manifest_hash,
            as_of=manifest.as_of.isoformat(),
            entries=tuple(asdict(entry) for entry in manifest.entries),
        )
        self._append_json(
            table="replay_manifests",
            key_column="manifest_hash",
            key=manifest.manifest_hash,
            payload=asdict(metadata),
        )

    def replay_manifests(self) -> tuple[ReplayManifestMetadata, ...]:
        rows = self._all_json("replay_manifests", "payload_json", "as_of, manifest_hash")
        return tuple(
            ReplayManifestMetadata(
                manifest_hash=str(payload["manifest_hash"]),
                as_of=str(payload["as_of"]),
                entries=tuple(payload["entries"]),
            )
            for payload in rows
        )

    def schema_version(self) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM audit_schema WHERE key = 'schema_version'"
            ).fetchone()
        if row is None:
            raise RuntimeError("audit schema version is missing")
        return str(row["value"])

    def integrity_check(self) -> AuditIntegrityReport:
        core_tables = (
            "decision_runs",
            "alerts",
            "alert_feedback",
            "point_in_time_records",
            "replay_manifests",
        )
        stream_tables = (
            "alert_stream_events",
            "stream_deliveries",
            "attribution_tasks",
            "attribution_reviews",
        )
        with self._lock:
            table_rows = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        installed_tables = {str(row[0]) for row in table_rows}
        discovered_stream_schema = bool(installed_tables.intersection(stream_tables))
        tables = core_tables + (stream_tables if discovered_stream_schema else ())
        if self._require_stream_schema:
            tables = core_tables + stream_tables
        expected_triggers = {
            f"{table}_{operation}"
            for table in tables
            for operation in ("deny_update", "deny_delete")
        }
        with self._lock:
            quick_rows = self._connection.execute("PRAGMA quick_check").fetchall()
            foreign_key_rows = self._connection.execute("PRAGMA foreign_key_check").fetchall()
            trigger_rows = self._connection.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        valid_triggers = {
            str(row["name"])
            for row in trigger_rows
            if self._is_expected_append_only_trigger(
                name=str(row["name"]),
                table=str(row["tbl_name"]),
                sql=str(row["sql"] or ""),
                expected_names=expected_triggers,
            )
        }
        return AuditIntegrityReport(
            schema_version=self.schema_version(),
            quick_check=tuple(str(row[0]) for row in quick_rows),
            foreign_key_violations=len(foreign_key_rows),
            append_only_triggers_installed=len(valid_triggers),
            append_only_triggers_expected=len(expected_triggers),
        )

    @staticmethod
    def _is_expected_append_only_trigger(
        *,
        name: str,
        table: str,
        sql: str,
        expected_names: set[str],
    ) -> bool:
        if name not in expected_names:
            return False
        operation = "update" if name.endswith("_deny_update") else "delete"
        expected_name = f"{table}_deny_{operation}"
        if name != expected_name:
            return False
        normalized = "".join(sql.casefold().split())
        denial_message = (
            "append-only phase4 audit table"
            if table
            in {
                "alert_stream_events",
                "stream_deliveries",
                "attribution_tasks",
                "attribution_reviews",
            }
            else "append-only audit table"
        )
        expected = "".join(
            (
                f"create trigger {name} before {operation} on {table} ",
                f"begin select raise(abort, '{denial_message}'); end",
            )
        )
        return normalized == "".join(expected.casefold().split())

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS audit_schema (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decision_runs (
                    run_id TEXT PRIMARY KEY,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    alert_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alert_feedback (
                    feedback_id TEXT PRIMARY KEY,
                    alert_id TEXT NOT NULL REFERENCES alerts(alert_id),
                    recorded_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alert_feedback_alert_idx
                    ON alert_feedback(alert_id, recorded_at);
                CREATE TABLE IF NOT EXISTS point_in_time_records (
                    record_id TEXT PRIMARY KEY,
                    first_seen_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replay_manifests (
                    manifest_hash TEXT PRIMARY KEY,
                    as_of TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                INSERT OR IGNORE INTO audit_schema(key, value)
                    VALUES ('schema_version', '1');
                COMMIT;
                """
            )
            for table in (
                "decision_runs",
                "alerts",
                "alert_feedback",
                "point_in_time_records",
                "replay_manifests",
            ):
                self._install_append_only_triggers(table)
        version = self.schema_version()
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported audit schema version: {version}; expected {SCHEMA_VERSION}"
            )

    def _install_append_only_triggers(self, table: str) -> None:
        self._connection.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_deny_update
            BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, 'append-only audit table'); END;
            CREATE TRIGGER IF NOT EXISTS {table}_deny_delete
            BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, 'append-only audit table'); END;
            """
        )

    def _append_json(
        self,
        *,
        table: str,
        key_column: str,
        key: str,
        payload: dict[str, Any],
        extra_columns: dict[str, str] | None = None,
    ) -> None:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        timestamp_column, timestamp = self._timestamp_for(table, payload)
        with self._lock, self._connection:
            existing = self._connection.execute(
                f"SELECT payload_json FROM {table} WHERE {key_column} = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_json"]) != serialized:
                    raise ImmutableAuditConflict(f"immutable audit conflict: {table}/{key}")
                return
            extra_columns = extra_columns or {}
            columns = [key_column, timestamp_column, "payload_json", *extra_columns]
            values = [key, timestamp, serialized, *extra_columns.values()]
            placeholders = ", ".join("?" for _ in columns)
            self._connection.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )

    @staticmethod
    def _timestamp_for(table: str, payload: dict[str, Any]) -> tuple[str, str]:
        if table == "alerts":
            return "created_at", str(payload["created_at"])
        if table == "alert_feedback":
            return "recorded_at", str(payload["recorded_at"])
        if table == "point_in_time_records":
            return "first_seen_at", str(payload["first_seen_at"])
        if table == "replay_manifests":
            return "as_of", str(payload["as_of"])
        return "recorded_at", str(payload["data_as_of"])

    def _get_json(self, table: str, key_column: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                f"SELECT payload_json FROM {table} WHERE {key_column} = ?",
                (key,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    def _all_json(
        self,
        table: str,
        column: str,
        order_by: str,
    ) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._connection.execute(
                f"SELECT {column} FROM {table} ORDER BY {order_by}"
            ).fetchall()
        return tuple(json.loads(row[column]) for row in rows)
