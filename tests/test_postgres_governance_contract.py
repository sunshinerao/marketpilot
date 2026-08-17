from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from marketpilot.domain.governance import (
    ApprovalAction,
    GovernanceApproval,
    GovernanceError,
    ModelVersion,
)
from marketpilot.services.persistence_contracts import (
    ChampionRegistry,
    GovernancePersistenceRepository,
)
from marketpilot.services.postgres_governance_store import (
    PostgreSQLFrozenChampionRegistry,
    PostgreSQLGovernanceStore,
)
from marketpilot.services.repository import ImmutableAuditConflict

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
MODEL_ID = "strikepilot_spxw_0dte_ic"


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.rows: list[Any] = []
        self.rowcount = 0

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> FakeCursor:
        sql = " ".join(query.split())
        self.rows = []
        self.rowcount = 0
        if "pg_advisory_xact_lock" in sql:
            self.rows = [(None,)]
            return self
        if "FROM pg_trigger" in sql:
            self.rows = [(8,)]
            return self

        insert = re.search(r"INSERT INTO marketpilot\.(\w+)\s*\(([^)]+)\)", sql)
        if insert:
            table = insert.group(1)
            columns = [value.strip() for value in insert.group(2).split(",")]
            row = dict(zip(columns, params, strict=True))
            key_columns = {
                "governance_model_versions": ("model_id", "version"),
                "governance_approvals": ("approval_id",),
                "governance_events": ("approval_id",),
                "governance_session_freezes": ("model_id", "session_id"),
            }[table]
            existing = self.connection.find(table, key_columns, row)
            if existing is not None:
                return self
            if table == "governance_events":
                self.connection.event_id += 1
                row["event_id"] = self.connection.event_id
            self.connection.rows(table).append(row)
            self.rowcount = 1
            return self

        table_match = re.search(r"FROM marketpilot\.(\w+)", sql)
        if table_match is None:
            raise AssertionError(f"unsupported SQL: {sql}")
        table = table_match.group(1)
        rows = list(self.connection.rows(table))
        if table == "governance_model_versions":
            rows = [row for row in rows if row["model_id"] == params[0]]
            if "version = %s" in sql:
                rows = [row for row in rows if row["version"] == params[1]]
        elif table == "governance_approvals" and "WHERE" in sql:
            rows = [row for row in rows if row["approval_id"] == params[0]]
        elif table == "governance_events":
            if "approval_id = %s" in sql:
                rows = [row for row in rows if row["approval_id"] == params[0]]
            elif "model_id = %s" in sql:
                rows = [row for row in rows if row["model_id"] == params[0]]
        elif table == "governance_session_freezes" and "WHERE" in sql:
            rows = [
                row
                for row in rows
                if row["model_id"] == params[0] and row["session_id"] == params[1]
            ]

        if "ORDER BY event_id DESC" in sql:
            rows.sort(key=lambda row: int(row["event_id"]), reverse=True)
            rows = rows[:1]
        elif "ORDER BY event_id" in sql:
            rows.sort(key=lambda row: int(row["event_id"]))
        elif "ORDER BY trained_at" in sql:
            rows.sort(key=lambda row: (str(row["trained_at"]), str(row["version"])))
        elif "ORDER BY approved_at" in sql:
            rows.sort(key=lambda row: (str(row["approved_at"]), str(row["approval_id"])))

        if sql.startswith("SELECT target_version"):
            self.rows = [(row["target_version"],) for row in rows]
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
        self.event_id = 0
        self.commits = 0
        self.rollbacks = 0

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self.tables.setdefault(table, [])

    def find(
        self,
        table: str,
        key_columns: tuple[str, ...],
        candidate: dict[str, Any],
    ) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.rows(table)
                if all(row[column] == candidate[column] for column in key_columns)
            ),
            None,
        )

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass


def version(name: str, *, parent: str | None = None) -> ModelVersion:
    return ModelVersion(
        model_id=MODEL_ID,
        version=name,
        artifact_hash=f"sha256:artifact-{name}",
        data_manifest_hash=f"sha256:data-{name}",
        trained_at=NOW,
        validation_report_hash=f"sha256:validation-{name}",
        parent_version=parent,
    )


def approval(
    action: ApprovalAction,
    target: str,
    *,
    source: str | None,
    evidence: str,
    minute: int,
) -> GovernanceApproval:
    return GovernanceApproval.create(
        action=action,
        model_id=MODEL_ID,
        source_version=source,
        target_version=target,
        approved_by="risk-committee",
        approved_at=NOW + timedelta(minutes=minute),
        evidence_hash=evidence,
        note="explicit durable approval",
    )


def durable_registry() -> tuple[
    PostgreSQLFrozenChampionRegistry, PostgreSQLGovernanceStore, FakeConnection
]:
    connection = FakeConnection()
    store = PostgreSQLGovernanceStore(lambda: connection)
    registry = PostgreSQLFrozenChampionRegistry(store, clock=lambda: NOW + timedelta(hours=1))
    return registry, store, connection


def test_durable_champion_events_approvals_and_session_freeze_survive_recreation() -> None:
    registry, store, connection = durable_registry()
    assert isinstance(registry, ChampionRegistry)
    assert isinstance(store, GovernancePersistenceRepository)
    first = version("1.0.0")
    second = version("1.1.0", parent="1.0.0")
    registry.register_challenger(first)
    first_approval = approval(
        ApprovalAction.PROMOTE,
        first.version,
        source=None,
        evidence=first.validation_report_hash or "",
        minute=1,
    )
    registry.promote(MODEL_ID, first.version, first_approval)
    assert registry.freeze_session(MODEL_ID, "2026-08-17").version == "1.0.0"

    registry.register_challenger(second)
    second_approval = approval(
        ApprovalAction.PROMOTE,
        second.version,
        source=first.version,
        evidence=second.validation_report_hash or "",
        minute=2,
    )
    registry.promote(MODEL_ID, second.version, second_approval)

    restarted_store = PostgreSQLGovernanceStore(lambda: connection)
    restarted = PostgreSQLFrozenChampionRegistry(restarted_store, clock=lambda: NOW)
    assert restarted.champion(MODEL_ID).version == "1.1.0"
    assert restarted.champion(MODEL_ID, session_id="2026-08-17").version == "1.0.0"
    assert restarted.audit_events() == registry.audit_events()
    assert restarted_store.approvals() == (first_approval, second_approval)
    assert restarted_store.verify_append_only_triggers()


def test_governance_rejects_wrong_evidence_reused_approval_and_source_race() -> None:
    registry, store, connection = durable_registry()
    first = version("1.0.0")
    registry.register_challenger(first)
    wrong = approval(
        ApprovalAction.PROMOTE,
        first.version,
        source=None,
        evidence="sha256:wrong",
        minute=1,
    )
    with pytest.raises(GovernanceError, match="evidence"):
        registry.promote(MODEL_ID, first.version, wrong)
    assert store.events() == ()
    assert store.approvals() == ()

    signed = approval(
        ApprovalAction.PROMOTE,
        first.version,
        source=None,
        evidence=first.validation_report_hash or "",
        minute=2,
    )
    registry.promote(MODEL_ID, first.version, signed)
    with pytest.raises(GovernanceError, match="already been used"):
        registry.promote(MODEL_ID, first.version, signed)

    second = version("1.1.0", parent=first.version)
    registry.register_challenger(second)
    stale = approval(
        ApprovalAction.PROMOTE,
        second.version,
        source=None,
        evidence=second.validation_report_hash or "",
        minute=3,
    )
    with pytest.raises(GovernanceError, match="current champion"):
        registry.promote(MODEL_ID, second.version, stale)
    assert len(connection.rows("governance_events")) == 1


def test_model_versions_are_idempotent_but_conflicting_content_and_orphans_fail() -> None:
    _, store, _ = durable_registry()
    first = version("1.0.0")
    store.append_model_version(first)
    store.append_model_version(first)
    with pytest.raises(ImmutableAuditConflict):
        store.append_model_version(replace(first, artifact_hash="sha256:changed"))
    with pytest.raises(GovernanceError, match="unknown parent"):
        store.append_model_version(version("2.0.0", parent="missing"))


def test_explicit_rollback_and_lineage_are_durable() -> None:
    registry, _, _ = durable_registry()
    first = version("1.0.0")
    second = version("1.1.0", parent=first.version)
    registry.register_challenger(first)
    registry.promote(
        MODEL_ID,
        first.version,
        approval(
            ApprovalAction.PROMOTE,
            first.version,
            source=None,
            evidence=first.validation_report_hash or "",
            minute=1,
        ),
    )
    registry.register_challenger(second)
    registry.promote(
        MODEL_ID,
        second.version,
        approval(
            ApprovalAction.PROMOTE,
            second.version,
            source=first.version,
            evidence=second.validation_report_hash or "",
            minute=2,
        ),
    )
    rollback = approval(
        ApprovalAction.ROLLBACK,
        first.version,
        source=second.version,
        evidence="sha256:incident-review",
        minute=3,
    )

    registry.rollback(MODEL_ID, first.version, rollback)

    assert registry.champion(MODEL_ID) == first
    assert [model.version for model in registry.lineage(MODEL_ID, second.version)] == [
        "1.1.0",
        "1.0.0",
    ]
    assert [event.action for event in registry.audit_events()] == [
        ApprovalAction.PROMOTE,
        ApprovalAction.PROMOTE,
        ApprovalAction.ROLLBACK,
    ]
