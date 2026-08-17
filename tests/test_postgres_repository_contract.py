from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from marketpilot.services.persistence_contracts import RecoveryCheckpoint
from marketpilot.services.postgres_repository import PostgreSQLAuditRepository
from marketpilot.services.raw_landing import LandingReceipt
from marketpilot.services.repository import ImmutableAuditConflict


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.rows: list[Any] = []
        self.rowcount = 0

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> FakeCursor:
        normalized = " ".join(query.split())
        if "FROM pg_trigger" in normalized:
            self.rows = [(self.connection.trigger_count,)]
            return self
        if "FROM pg_constraint" in normalized:
            self.rows = [(self.connection.unvalidated_constraints,)]
            return self
        insert = re.search(r"INSERT INTO marketpilot\.(\w+) \(([^)]+)\)", normalized)
        if insert:
            table = insert.group(1)
            columns = [column.strip() for column in insert.group(2).split(",")]
            row = dict(zip(columns, params, strict=True))
            key = str(params[0])
            if key in self.connection.tables.setdefault(table, {}):
                self.rowcount = 0
            else:
                self.connection.tables[table][key] = row
                self.rowcount = 1
            self.rows = []
            return self

        table_match = re.search(r"FROM marketpilot\.(\w+)", normalized)
        if not table_match:
            raise AssertionError(f"unsupported SQL: {normalized}")
        table = table_match.group(1)
        if table == "audit_schema":
            self.rows = [("1",)]
            return self
        values = list(self.connection.tables.get(table, {}).values())
        if "WHERE" in normalized:
            values = [row for row in values if str(next(iter(row.values()))) == str(params[0])]
        if "ORDER BY captured_at DESC" in normalized:
            values.sort(key=lambda row: str(row["captured_at"]), reverse=True)
        if "LIMIT %s" in normalized:
            values = values[: int(params[-1])]
        if normalized.startswith("SELECT 1"):
            self.rows = [(1,)] if values else []
        else:
            self.rows = [(row["payload_json"],) for row in values]
        return self

    def fetchone(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return self.rows

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self) -> None:
        self.tables: dict[str, dict[str, dict[str, Any]]] = {}
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.trigger_count = 30
        self.unvalidated_constraints = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def receipt(*, algorithm: str = "AES-256-GCM") -> LandingReceipt:
    return LandingReceipt(
        landing_id="landing-1",
        object_key="licensed/feed/events/landing-1",
        provider="feed",
        dataset="events",
        logical_key_hash="sha256-logical",
        published_at=datetime(2026, 8, 17, 12, tzinfo=UTC),
        first_seen_at=datetime(2026, 8, 17, 12, 0, 1, tzinfo=UTC),
        plaintext_sha256="sha256-payload",
        key_id="kms/key/v1",
        algorithm=algorithm,
        content_type="application/json",
    )


def checkpoint(checkpoint_id: str, minute: int) -> RecoveryCheckpoint:
    return RecoveryCheckpoint(
        checkpoint_id=checkpoint_id,
        captured_at=datetime(2026, 8, 17, 12, minute, tzinfo=UTC),
        database_lsn=f"0/{minute:02X}",
        backup_reference=f"backup://audit/{checkpoint_id}",
        manifest_hash=f"manifest-{checkpoint_id}",
        code_version="git:abc123",
        schema_version="1",
    )


def test_postgres_adapter_is_idempotent_conflict_aware_and_transactional() -> None:
    connection = FakeConnection()
    repository = PostgreSQLAuditRepository(lambda: connection)

    repository.append(receipt())
    repository.append(receipt())
    assert connection.commits == 2
    assert connection.rollbacks == 0

    with pytest.raises(ImmutableAuditConflict, match="raw_landing_receipts/landing-1"):
        repository.append(receipt(algorithm="changed"))
    assert connection.rollbacks == 1
    stored = connection.tables["raw_landing_receipts"]["landing-1"]["payload_json"]
    assert "ciphertext" not in stored
    assert "nonce" not in stored


def test_recovery_checkpoints_are_append_only_and_latest_is_restored() -> None:
    connection = FakeConnection()
    repository = PostgreSQLAuditRepository(lambda: connection)
    repository.append_recovery_checkpoint(checkpoint("checkpoint-a", 1))
    repository.append_recovery_checkpoint(checkpoint("checkpoint-b", 2))

    assert repository.latest_recovery_checkpoint() == checkpoint("checkpoint-b", 2)
    repository.append_recovery_checkpoint(checkpoint("checkpoint-b", 2))
    with pytest.raises(ImmutableAuditConflict):
        repository.append_recovery_checkpoint(
            replace(
                checkpoint("checkpoint-b", 2),
                backup_reference="backup://different",
            )
        )


def test_schema_verification_and_connection_lifecycle() -> None:
    connection = FakeConnection()
    repository = PostgreSQLAuditRepository(lambda: connection)
    repository.verify_schema()
    assert repository.integrity_check().ok
    repository.close()
    assert connection.closed


def test_postgres_decision_history_matches_shared_repository_contract() -> None:
    from marketpilot.services.schemas import DecisionRunOutput

    connection = FakeConnection()
    repository = PostgreSQLAuditRepository(lambda: connection)
    item = DecisionRunOutput(
        run_id="run-1",
        run_mode="SCENARIO",
        model_id="model",
        model_version="model-v1",
        rules_version="rules-v1",
        code_version="code-v1",
        snapshot_id="sha256:snapshot",
        data_as_of=datetime(2026, 8, 17, 12, tzinfo=UTC),
        action="NO_TRADE",
        reasons=["EVENT_PENDING"],
        output={},
    )
    repository.append_decision(item)

    assert repository.decisions(limit=1) == (item,)
    with pytest.raises(ValueError, match=r"\[1, 1000\]"):
        repository.decisions(limit=0)


def test_integrity_report_fails_closed_for_missing_triggers_or_unvalidated_fk() -> None:
    connection = FakeConnection()
    connection.trigger_count = 28
    connection.unvalidated_constraints = 1
    repository = PostgreSQLAuditRepository(lambda: connection)

    report = repository.integrity_check()

    assert not report.ok
    assert report.quick_check == ("append_only_triggers:28/30",)
    assert report.foreign_key_violations == 1


def test_postgres_migration_enforces_append_only_and_payload_separation() -> None:
    migration = Path("migrations/postgresql/0001_audit.sql").read_text()

    assert "deny_audit_mutation" in migration
    assert "BEFORE UPDATE" in migration
    assert "BEFORE DELETE" in migration
    assert "pit_metadata_has_no_canonical_content" in migration
    assert "raw_receipt_forbids_payload_fields" in migration
    for forbidden in ("'payload'", "'plaintext'", "'ciphertext'", "'nonce'"):
        assert forbidden in migration
