from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Never

import pytest

from marketpilot.governance.registry import FrozenChampionRegistry
from marketpilot.services import runtime_persistence as runtime_module
from marketpilot.services.repository import SQLiteAuditRepository
from marketpilot.services.runtime_persistence import create_runtime_persistence
from marketpilot.services.stream_attribution_store import StreamAttributionStore


def test_sqlite_runtime_uses_one_durable_path(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    runtime = create_runtime_persistence(
        {
            "MARKETPILOT_AUDIT_BACKEND": "sqlite",
            "MARKETPILOT_AUDIT_DB": str(path),
        }
    )
    try:
        assert runtime.backend == "sqlite"
        assert isinstance(runtime.audit, SQLiteAuditRepository)
        assert isinstance(runtime.stream_attribution, StreamAttributionStore)
        assert isinstance(runtime.governance, FrozenChampionRegistry)
        assert path.exists()
        assert runtime.audit.integrity_check().ok is True
    finally:
        runtime.close()


def test_sqlite_runtime_refuses_tampered_trigger_before_write_open(tmp_path: Path) -> None:
    path = tmp_path / "tampered.sqlite3"
    repository = SQLiteAuditRepository(path)
    repository.close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER decision_runs_deny_update")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="integrity check failed before startup"):
        create_runtime_persistence(
            {
                "MARKETPILOT_AUDIT_BACKEND": "sqlite",
                "MARKETPILOT_AUDIT_DB": str(path),
            }
        )

    trigger_count = sqlite3.connect(path).execute(
        "SELECT count(*) FROM sqlite_master "
        "WHERE type = 'trigger' AND name = 'decision_runs_deny_update'"
    ).fetchone()
    assert trigger_count == (0,)


def test_sqlite_runtime_refuses_counterfeit_stream_trigger_before_write_open(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tampered-stream.sqlite3"
    runtime = create_runtime_persistence(
        {
            "MARKETPILOT_AUDIT_BACKEND": "sqlite",
            "MARKETPILOT_AUDIT_DB": str(path),
        }
    )
    runtime.close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER stream_deliveries_deny_update")
    connection.execute(
        "CREATE TRIGGER stream_deliveries_deny_update "
        "AFTER INSERT ON stream_deliveries BEGIN SELECT 1; END"
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="integrity check failed before startup"):
        create_runtime_persistence(
            {
                "MARKETPILOT_AUDIT_BACKEND": "sqlite",
                "MARKETPILOT_AUDIT_DB": str(path),
            }
        )


def test_sqlite_runtime_refuses_deleted_stream_schema_before_reinitializing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deleted-stream.sqlite3"
    runtime = create_runtime_persistence(
        {
            "MARKETPILOT_AUDIT_BACKEND": "sqlite",
            "MARKETPILOT_AUDIT_DB": str(path),
        }
    )
    runtime.close()
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        DROP TABLE attribution_reviews;
        DROP TABLE attribution_tasks;
        DROP TABLE stream_deliveries;
        DROP TABLE alert_stream_events;
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="integrity check failed before startup"):
        create_runtime_persistence(
            {
                "MARKETPILOT_AUDIT_BACKEND": "sqlite",
                "MARKETPILOT_AUDIT_DB": str(path),
            }
        )


def test_postgresql_runtime_requires_secret_dsn_before_importing_driver() -> None:
    with pytest.raises(RuntimeError, match="MARKETPILOT_POSTGRES_DSN is required"):
        create_runtime_persistence({"MARKETPILOT_AUDIT_BACKEND": "postgresql"})


def test_unknown_runtime_backend_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="must be sqlite or postgresql"):
        create_runtime_persistence({"MARKETPILOT_AUDIT_BACKEND": "memory-magic"})


def test_postgresql_startup_error_does_not_expose_secret_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "postgresql://runtime:super-secret@example.invalid/marketpilot"

    def broken_factory(_: str) -> Callable[[], Never]:
        def connect() -> Never:
            raise RuntimeError(f"could not connect using {secret}")

        return connect

    monkeypatch.setattr(runtime_module, "psycopg_connection_factory", broken_factory)
    with pytest.raises(RuntimeError) as error:
        create_runtime_persistence(
            {
                "MARKETPILOT_AUDIT_BACKEND": "postgresql",
                "MARKETPILOT_POSTGRES_DSN": secret,
            }
        )
    assert "details omitted" in str(error.value)
    assert "super-secret" not in str(error.value)
