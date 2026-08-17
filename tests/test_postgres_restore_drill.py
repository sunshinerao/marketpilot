from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import marketpilot.restore_drill as drill
from marketpilot.restore_drill import (
    EXPECTED_FOREIGN_KEYS,
    EXPECTED_TABLES,
    EXPECTED_TRIGGERS,
    DrillResources,
    _sha256,
    build_plan,
    main,
)


def test_restore_drill_plan_is_isolated_from_existing_compose_volumes() -> None:
    resources = DrillResources.create()
    plan = build_plan(
        resources,
        "postgres:17-alpine",
        Path("migrations/postgresql/0001_audit.sql").resolve(),
    )

    assert plan["isolation"]["published_ports"] is False
    assert plan["isolation"]["existing_compose_volumes_mounted"] is False
    assert plan["isolation"]["source"]["volume"].startswith(
        "marketpilot-restore-drill-"
    )
    assert plan["isolation"]["restore"]["volume"].startswith(
        "marketpilot-restore-drill-"
    )
    assert "marketpilot-postgres" not in str(plan)
    assert "decision_hash" in plan["checks"]
    assert "governance_hash" in plan["checks"]
    assert "checkpoint_hash_and_manifest_link" in plan["checks"]


def test_restore_drill_plan_mode_does_not_contact_docker() -> None:
    assert main(["--plan"]) == 0


def test_restore_verification_expectations_match_migration_contract() -> None:
    migration = Path("migrations/postgresql/0001_audit.sql").read_text(encoding="utf-8")
    trigger_table_block = migration.split(
        "FOREACH table_name IN ARRAY ARRAY[", maxsplit=1
    )[1].split("]", maxsplit=1)[0]
    trigger_tables = re.findall(r"'[a-z_]+'", trigger_table_block)

    assert migration.count("CREATE TABLE IF NOT EXISTS") == EXPECTED_TABLES
    assert len(trigger_tables) * 2 == EXPECTED_TRIGGERS
    assert migration.count("REFERENCES marketpilot.") == EXPECTED_FOREIGN_KEYS
    assert "CREATE TRIGGER deny_update" in migration
    assert "CREATE TRIGGER deny_delete" in migration


def test_snapshot_hash_is_deterministic_and_content_sensitive() -> None:
    assert _sha256("same") == _sha256("same")
    assert _sha256("same") != _sha256("different")


def test_restore_verifier_requires_the_complete_database_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def sql_value(_container: str, sql: str) -> str:
        if "audit_schema" in sql and "SELECT value" in sql:
            return "1"
        if "information_schema.tables" in sql:
            return str(EXPECTED_TABLES)
        if "FROM pg_trigger" in sql:
            return str(EXPECTED_TRIGGERS)
        if "FROM pg_constraint" in sql:
            return f"{EXPECTED_FOREIGN_KEYS}|0"
        if "recovery_checkpoints" in sql:
            return "1"
        raise AssertionError(sql)

    monkeypatch.setattr(drill, "_sql_value", sql_value)
    monkeypatch.setattr(
        drill,
        "_expect_database_rejection",
        lambda _container, _sql, _marker: True,
    )

    verification = drill._verify_restored_database("restored")

    assert all(verification["checks"].values())
    assert verification["observed"]["append_only_trigger_count"] == 30


def test_restore_helpers_fail_closed_and_cleanup_only_labeled_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = subprocess.CompletedProcess(["docker"], 7, "", "safe failure")
    monkeypatch.setattr(drill.subprocess, "run", lambda *args, **kwargs: failed)
    with pytest.raises(drill.DrillError, match="safe failure"):
        drill._run(("docker", "version"))

    resources = DrillResources.create()
    removed: set[tuple[str, str]] = set()

    monkeypatch.setattr(drill, "_resource_has_label", lambda *_args: True)
    monkeypatch.setattr(
        drill,
        "_resource_exists",
        lambda kind, name: (kind, name) not in removed,
    )

    def docker(*args: str, **_kwargs: object) -> str:
        kind = "container" if args[0] == "container" else "volume"
        removed.add((kind, args[-1]))
        return ""

    monkeypatch.setattr(drill, "_docker", docker)
    assert drill._cleanup(resources) is True
    assert len(removed) == 4


def test_postgres_start_and_readiness_helpers_build_isolated_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docker_calls: list[tuple[str, ...]] = []

    def record_docker(*args: str, **_kwargs: object) -> str:
        docker_calls.append(args)
        return ""

    monkeypatch.setattr(
        drill,
        "_docker",
        record_docker,
    )
    migration = tmp_path / "migration.sql"
    migration.write_text("-- fixture", encoding="utf-8")
    drill._start_postgres(
        name="source",
        volume="isolated-volume",
        run_id="run-id",
        image="postgres:17-alpine",
        migration=migration,
    )
    command = docker_calls[0]
    assert "--detach" in command
    assert "POSTGRES_HOST_AUTH_METHOD=trust" in command
    assert "127.0.0.1" not in command
    assert str(migration) in " ".join(command)

    ready = subprocess.CompletedProcess(["pg_isready"], 0, "", "")
    monkeypatch.setattr(drill, "_run", lambda *args, **kwargs: ready)
    drill._wait_ready("source", attempts=1)
