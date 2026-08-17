from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from marketpilot.governance.registry import FrozenChampionRegistry
from marketpilot.services.persistence_contracts import (
    AuditRepository,
    ChampionRegistry,
    GovernancePersistenceRepository,
    StreamAttributionRepository,
)
from marketpilot.services.postgres_governance_store import (
    PostgreSQLFrozenChampionRegistry,
    PostgreSQLGovernanceStore,
)
from marketpilot.services.postgres_repository import (
    PostgreSQLAuditRepository,
    psycopg_connection_factory,
)
from marketpilot.services.postgres_stream_attribution_store import (
    PostgreSQLStreamAttributionStore,
)
from marketpilot.services.repository import SQLiteAuditRepository
from marketpilot.services.stream_attribution_store import StreamAttributionStore


@dataclass(frozen=True, slots=True)
class RuntimePersistence:
    backend: Literal["sqlite", "postgresql"]
    audit: AuditRepository
    stream_attribution: StreamAttributionRepository
    governance: ChampionRegistry
    governance_store: GovernancePersistenceRepository | None = None

    def close(self) -> None:
        if self.governance_store is not None:
            self.governance_store.close()
        self.stream_attribution.close()
        self.audit.close()


def create_runtime_persistence(environment: Mapping[str, str]) -> RuntimePersistence:
    """Create one explicit storage backend and verify it before serving traffic."""

    backend = environment.get("MARKETPILOT_AUDIT_BACKEND", "sqlite").strip().lower()
    if backend == "sqlite":
        path = environment.get("MARKETPILOT_AUDIT_DB", "data/audit/marketpilot.sqlite3")
        existing_database = path != ":memory:" and Path(path).exists()
        # Separate sqlite3 ':memory:' connections cannot share one audit schema and are
        # test-only/non-durable. Every file-backed runtime requires the complete core +
        # stream schema before it can report integrity PASS.
        require_stream_schema = path != ":memory:"
        if existing_database:
            probe = SQLiteAuditRepository(
                path,
                initialize=False,
                read_only=True,
                require_stream_schema=require_stream_schema,
            )
            try:
                if not probe.integrity_check().ok:
                    raise RuntimeError("SQLite audit integrity check failed before startup")
            finally:
                probe.close()
        sqlite_audit = SQLiteAuditRepository(
            path,
            initialize=not existing_database,
            require_stream_schema=require_stream_schema,
        )
        sqlite_stream = StreamAttributionStore(path)
        if not sqlite_audit.integrity_check().ok:
            sqlite_stream.close()
            sqlite_audit.close()
            raise RuntimeError("SQLite audit integrity check failed")
        return RuntimePersistence(
            backend="sqlite",
            audit=sqlite_audit,
            stream_attribution=sqlite_stream,
            governance=FrozenChampionRegistry(),
        )

    if backend == "postgresql":
        secret_dsn = environment.get("MARKETPILOT_POSTGRES_DSN", "")
        if not secret_dsn.strip():
            raise RuntimeError("MARKETPILOT_POSTGRES_DSN is required for PostgreSQL")
        try:
            connection_factory = psycopg_connection_factory(secret_dsn)
            postgres_audit = PostgreSQLAuditRepository(connection_factory)
            report = postgres_audit.integrity_check()
        except Exception:
            # Provider exceptions may include connection details. Never expose them in
            # startup logs or HTTP responses.
            raise RuntimeError("PostgreSQL audit startup failed; details omitted") from None
        if not report.ok:
            postgres_audit.close()
            raise RuntimeError("PostgreSQL audit integrity check failed")
        try:
            postgres_stream = PostgreSQLStreamAttributionStore(connection_factory)
            stream_ok = postgres_stream.verify_append_only_triggers()
        except Exception:
            postgres_audit.close()
            raise RuntimeError("PostgreSQL stream startup failed; details omitted") from None
        if not stream_ok:
            postgres_stream.close()
            postgres_audit.close()
            raise RuntimeError("PostgreSQL stream integrity check failed")
        try:
            postgres_governance_store = PostgreSQLGovernanceStore(connection_factory)
            governance_ok = postgres_governance_store.verify_append_only_triggers()
        except Exception:
            postgres_stream.close()
            postgres_audit.close()
            raise RuntimeError("PostgreSQL governance startup failed; details omitted") from None
        if not governance_ok:
            postgres_governance_store.close()
            postgres_stream.close()
            postgres_audit.close()
            raise RuntimeError("PostgreSQL governance integrity check failed")
        return RuntimePersistence(
            backend="postgresql",
            audit=postgres_audit,
            stream_attribution=postgres_stream,
            governance=PostgreSQLFrozenChampionRegistry(postgres_governance_store),
            governance_store=postgres_governance_store,
        )

    raise RuntimeError("MARKETPILOT_AUDIT_BACKEND must be sqlite or postgresql")
