from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

DRILL_LABEL = "marketpilot.restore-drill"
DEFAULT_IMAGE = "postgres:17-alpine"
DATABASE = "marketpilot_restore"
DATABASE_USER = "marketpilot_restore"
EXPECTED_TABLES = 16
EXPECTED_TRIGGERS = 30
EXPECTED_FOREIGN_KEYS = 8

_SNAPSHOT_QUERIES = {
    "decision_hash": """
        SELECT run_id || '|' || payload_json::text
        FROM marketpilot.decision_runs ORDER BY run_id
    """,
    "manifest_hash": """
        SELECT manifest_hash || '|' || payload_json::text
        FROM marketpilot.replay_manifests ORDER BY manifest_hash
    """,
    "checkpoint_hash": """
        SELECT checkpoint_id || '|' || payload_json::text
        FROM marketpilot.recovery_checkpoints ORDER BY checkpoint_id
    """,
    "governance_hash": """
        SELECT value FROM (
            SELECT 'model|' || model_id || '|' || version || '|' || payload_json::text AS value
            FROM marketpilot.governance_model_versions
            UNION ALL
            SELECT 'approval|' || approval_id || '|' || payload_json::text
            FROM marketpilot.governance_approvals
            UNION ALL
            SELECT 'event|' || approval_id || '|' || payload_json::text
            FROM marketpilot.governance_events
            UNION ALL
            SELECT 'freeze|' || model_id || '|' || session_id || '|' || payload_json::text
            FROM marketpilot.governance_session_freezes
        ) AS governance_rows ORDER BY value
    """,
}

_FIXTURE_SQL = """
INSERT INTO marketpilot.decision_runs(run_id, recorded_at, payload_json)
VALUES (
    'restore-drill-decision',
    '2026-08-16T02:00:00Z',
    '{"action":"NO_TRADE","data_as_of":"2026-08-16T02:00:00Z","run_id":"restore-drill-decision","snapshot_hash":"sha256:restore-drill-snapshot"}'::jsonb
);

INSERT INTO marketpilot.replay_manifests(manifest_hash, as_of, payload_json)
VALUES (
    'sha256:restore-drill-manifest',
    '2026-08-16T02:00:00Z',
    '{"as_of":"2026-08-16T02:00:00Z","entries":[],"manifest_hash":"sha256:restore-drill-manifest"}'::jsonb
);

INSERT INTO marketpilot.alerts(alert_id, created_at, payload_json)
VALUES (
    'restore-drill-alert',
    '2026-08-16T02:00:00Z',
    '{"alert_id":"restore-drill-alert","state":"RISK_LOCK"}'::jsonb
);

INSERT INTO marketpilot.governance_model_versions(
    model_id, version, parent_version, trained_at, payload_json
) VALUES
(
    'strikepilot', '0.1.0-baseline', NULL, '2026-08-15T01:00:00Z',
    '{"artifact_hash":"sha256:restore-drill-baseline","model_id":"strikepilot","parent_version":null,"trained_at":"2026-08-15T01:00:00Z","validation_report_hash":null,"version":"0.1.0-baseline"}'::jsonb
),
(
    'strikepilot', '1.0.0-restore-drill', '0.1.0-baseline', '2026-08-16T01:00:00Z',
    '{"artifact_hash":"sha256:restore-drill-artifact","model_id":"strikepilot","parent_version":"0.1.0-baseline","trained_at":"2026-08-16T01:00:00Z","validation_report_hash":"sha256:restore-drill-validation","version":"1.0.0-restore-drill"}'::jsonb
);

INSERT INTO marketpilot.governance_approvals(
    approval_id, model_id, approved_at, payload_json
) VALUES (
    'restore-drill-approval', 'strikepilot', '2026-08-16T01:30:00Z',
    '{"action":"PROMOTE","approval_id":"restore-drill-approval","approved_at":"2026-08-16T01:30:00Z","evidence_hash":"sha256:restore-drill-validation","model_id":"strikepilot","source_version":null,"target_version":"1.0.0-restore-drill"}'::jsonb
);

INSERT INTO marketpilot.governance_events(
    approval_id, model_id, source_version, target_version, occurred_at, payload_json
) VALUES (
    'restore-drill-approval', 'strikepilot', NULL, '1.0.0-restore-drill',
    '2026-08-16T01:30:00Z',
    '{"action":"PROMOTE","approval_id":"restore-drill-approval","model_id":"strikepilot","occurred_at":"2026-08-16T01:30:00Z","source_version":null,"target_version":"1.0.0-restore-drill"}'::jsonb
);

INSERT INTO marketpilot.governance_session_freezes(
    model_id, session_id, version, frozen_at, payload_json
) VALUES (
    'strikepilot', '2026-08-16-RTH', '1.0.0-restore-drill', '2026-08-16T01:45:00Z',
    '{"frozen_at":"2026-08-16T01:45:00Z","model_id":"strikepilot","session_id":"2026-08-16-RTH","version":"1.0.0-restore-drill"}'::jsonb
);

INSERT INTO marketpilot.recovery_checkpoints(
    checkpoint_id, captured_at, payload_json
)
SELECT
    'restore-drill-checkpoint',
    '2026-08-16T02:00:00Z',
    jsonb_build_object(
        'backup_reference', 'restore-drill://fixture',
        'captured_at', '2026-08-16T02:00:00+00:00',
        'checkpoint_id', 'restore-drill-checkpoint',
        'code_version', 'restore-drill-fixture-v1',
        'database_lsn', pg_current_wal_lsn()::text,
        'manifest_hash', 'sha256:restore-drill-manifest',
        'schema_version', '1'
    );
"""


@dataclass(frozen=True, slots=True)
class DrillResources:
    run_id: str
    source_container: str
    restore_container: str
    source_volume: str
    restore_volume: str

    @classmethod
    def create(cls) -> DrillResources:
        run_id = secrets.token_hex(8)
        prefix = f"marketpilot-restore-drill-{run_id}"
        return cls(
            run_id=run_id,
            source_container=f"{prefix}-source",
            restore_container=f"{prefix}-restored",
            source_volume=f"{prefix}-source-data",
            restore_volume=f"{prefix}-restored-data",
        )


class DrillError(RuntimeError):
    """A safe, non-secret-bearing restore-drill failure."""


def build_plan(resources: DrillResources, image: str, migration: Path) -> dict[str, Any]:
    return {
        "run_id": resources.run_id,
        "image": image,
        "migration": str(migration),
        "isolation": {
            "published_ports": False,
            "source": {
                "container": resources.source_container,
                "volume": resources.source_volume,
            },
            "restore": {
                "container": resources.restore_container,
                "volume": resources.restore_volume,
            },
            "existing_compose_volumes_mounted": False,
        },
        "checks": [
            "schema_version",
            "table_count",
            "append_only_trigger_count",
            "foreign_key_count_and_validation",
            "foreign_key_enforcement",
            "update_rejected",
            "delete_rejected",
            "decision_hash",
            "governance_hash",
            "manifest_hash",
            "checkpoint_hash_and_manifest_link",
        ],
    }


def _run(
    args: Sequence[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(args),
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise DrillError(f"unable to start {args[0]} operation: {error.strerror}") from error
    if check and completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        summary = detail[-1] if detail else "command returned a non-zero exit status"
        raise DrillError(f"{args[0]} operation failed: {summary}")
    return completed


def _docker(*args: str, input_text: str | None = None, check: bool = True) -> str:
    return _run(("docker", *args), input_text=input_text, check=check).stdout.strip()


def _start_postgres(
    *,
    name: str,
    volume: str,
    run_id: str,
    image: str,
    migration: Path | None,
) -> None:
    args = [
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        f"{DRILL_LABEL}={run_id}",
        "--mount",
        f"type=volume,source={volume},target=/var/lib/postgresql/data",
        "--env",
        f"POSTGRES_DB={DATABASE}",
        "--env",
        f"POSTGRES_USER={DATABASE_USER}",
        "--env",
        "POSTGRES_HOST_AUTH_METHOD=trust",
    ]
    if migration is not None:
        args.extend(
            (
                "--mount",
                f"type=bind,source={migration},target=/docker-entrypoint-initdb.d/0001_audit.sql,readonly",
            )
        )
    args.append(image)
    _docker(*args)


def _wait_ready(container: str, *, attempts: int = 60) -> None:
    for _ in range(attempts):
        completed = _run(
            (
                "docker",
                "exec",
                container,
                "pg_isready",
                "-U",
                DATABASE_USER,
                "-d",
                DATABASE,
            ),
            check=False,
        )
        if completed.returncode == 0:
            return
        time.sleep(1)
    raise DrillError(f"temporary PostgreSQL container did not become ready: {container}")


def _psql(container: str, sql: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(
        (
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            DATABASE_USER,
            "-d",
            DATABASE,
        ),
        input_text=sql,
        check=check,
    )


def _sql_value(container: str, sql: str) -> str:
    return _psql(container, sql).stdout.strip()


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _snapshot(container: str) -> dict[str, str]:
    return {
        name: _sha256(_sql_value(container, query))
        for name, query in _SNAPSHOT_QUERIES.items()
    }


def _expect_database_rejection(container: str, sql: str, marker: str) -> bool:
    result = _psql(container, sql, check=False)
    output = f"{result.stdout}\n{result.stderr}".lower()
    return result.returncode != 0 and marker.lower() in output


def _verify_restored_database(container: str) -> dict[str, Any]:
    schema_version = _sql_value(
        container,
        "SELECT value FROM marketpilot.audit_schema WHERE key = 'schema_version';",
    )
    table_count = int(
        _sql_value(
            container,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'marketpilot' AND table_type = 'BASE TABLE';",
        )
    )
    trigger_count = int(
        _sql_value(
            container,
            """
            SELECT count(*)
            FROM pg_trigger t
            JOIN pg_class r ON r.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = r.relnamespace
            JOIN pg_proc p ON p.oid = t.tgfoid
            WHERE n.nspname = 'marketpilot'
              AND p.proname = 'deny_audit_mutation'
              AND NOT t.tgisinternal
              AND t.tgenabled <> 'D';
            """,
        )
    )
    foreign_key_parts = _sql_value(
        container,
        """
        SELECT count(*) || '|' || count(*) FILTER (WHERE NOT c.convalidated)
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE n.nspname = 'marketpilot' AND c.contype = 'f';
        """,
    ).split("|")
    foreign_key_count, unvalidated_foreign_keys = (int(value) for value in foreign_key_parts)
    update_rejected = _expect_database_rejection(
        container,
        "UPDATE marketpilot.alerts SET payload_json = '{}'::jsonb "
        "WHERE alert_id = 'restore-drill-alert';",
        "append-only audit table",
    )
    delete_rejected = _expect_database_rejection(
        container,
        "DELETE FROM marketpilot.alerts WHERE alert_id = 'restore-drill-alert';",
        "append-only audit table",
    )
    foreign_key_enforced = _expect_database_rejection(
        container,
        """
        INSERT INTO marketpilot.alert_feedback(
            feedback_id, alert_id, recorded_at, payload_json
        ) VALUES (
            'restore-drill-invalid-feedback', 'missing-alert', now(), '{}'::jsonb
        );
        """,
        "foreign key constraint",
    )
    checkpoint_link_count = int(
        _sql_value(
            container,
            """
            SELECT count(*)
            FROM marketpilot.recovery_checkpoints c
            JOIN marketpilot.replay_manifests m
              ON m.manifest_hash = c.payload_json->>'manifest_hash'
            JOIN marketpilot.audit_schema s
              ON s.key = 'schema_version'
             AND s.value = c.payload_json->>'schema_version'
            WHERE c.checkpoint_id = 'restore-drill-checkpoint';
            """,
        )
    )
    checks = {
        "schema_version": schema_version == "1",
        "table_count": table_count == EXPECTED_TABLES,
        "append_only_trigger_count": trigger_count == EXPECTED_TRIGGERS,
        "foreign_key_count": foreign_key_count == EXPECTED_FOREIGN_KEYS,
        "foreign_keys_validated": unvalidated_foreign_keys == 0,
        "foreign_key_enforced": foreign_key_enforced,
        "update_rejected": update_rejected,
        "delete_rejected": delete_rejected,
        "checkpoint_manifest_schema_link": checkpoint_link_count == 1,
    }
    return {
        "checks": checks,
        "observed": {
            "schema_version": schema_version,
            "table_count": table_count,
            "append_only_trigger_count": trigger_count,
            "foreign_key_count": foreign_key_count,
            "unvalidated_foreign_keys": unvalidated_foreign_keys,
            "checkpoint_link_count": checkpoint_link_count,
        },
    }


def _resource_has_label(kind: str, name: str, run_id: str) -> bool:
    label_path = ".Config.Labels" if kind == "container" else ".Labels"
    result = _run(
        (
            "docker",
            kind,
            "inspect",
            "--format",
            f"{{{{ index {label_path} \"{DRILL_LABEL}\" }}}}",
            name,
        ),
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == run_id


def _resource_exists(kind: str, name: str) -> bool:
    return _run(("docker", kind, "inspect", name), check=False).returncode == 0


def _cleanup(resources: DrillResources) -> bool:
    cleaned = True
    for container in (resources.source_container, resources.restore_container):
        if _resource_has_label("container", container, resources.run_id):
            _docker("container", "rm", "--force", container, check=False)
        if _resource_exists("container", container):
            cleaned = False
    for volume in (resources.source_volume, resources.restore_volume):
        if _resource_has_label("volume", volume, resources.run_id):
            _docker("volume", "rm", volume, check=False)
        if _resource_exists("volume", volume):
            cleaned = False
    return cleaned


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_restore_drill(*, image: str, migration: Path, report_path: Path) -> dict[str, Any]:
    if not migration.is_file():
        raise DrillError(f"migration file does not exist: {migration}")
    resources = DrillResources.create()
    plan = build_plan(resources, image, migration)
    report: dict[str, Any] = {"plan": plan, "status": "FAIL"}
    try:
        _run(("docker", "version", "--format", "{{.Server.Version}}"))
        for volume in (resources.source_volume, resources.restore_volume):
            _docker(
                "volume",
                "create",
                "--label",
                f"{DRILL_LABEL}={resources.run_id}",
                volume,
            )
            if not _resource_has_label("volume", volume, resources.run_id):
                raise DrillError("temporary volume label verification failed")
        _start_postgres(
            name=resources.source_container,
            volume=resources.source_volume,
            run_id=resources.run_id,
            image=image,
            migration=migration,
        )
        _wait_ready(resources.source_container)
        _psql(resources.source_container, _FIXTURE_SQL)
        source_snapshot = _snapshot(resources.source_container)

        with TemporaryDirectory(prefix="marketpilot-restore-drill-") as temp_dir:
            dump_path = Path(temp_dir) / "marketpilot.dump"
            _docker(
                "exec",
                resources.source_container,
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--file=/tmp/marketpilot.dump",
                "-U",
                DATABASE_USER,
                "-d",
                DATABASE,
            )
            _docker(
                "cp",
                f"{resources.source_container}:/tmp/marketpilot.dump",
                str(dump_path),
            )

            _start_postgres(
                name=resources.restore_container,
                volume=resources.restore_volume,
                run_id=resources.run_id,
                image=image,
                migration=None,
            )
            _wait_ready(resources.restore_container)
            _docker("cp", str(dump_path), f"{resources.restore_container}:/tmp/marketpilot.dump")
            _docker(
                "exec",
                resources.restore_container,
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "--dbname",
                DATABASE,
                "--username",
                DATABASE_USER,
                "/tmp/marketpilot.dump",
            )

        restored_snapshot = _snapshot(resources.restore_container)
        verification = _verify_restored_database(resources.restore_container)
        hashes_match = source_snapshot == restored_snapshot
        all_checks = all(verification["checks"].values()) and hashes_match
        report.update(
            {
                "status": "PASS" if all_checks else "FAIL",
                "source_hashes": source_snapshot,
                "restored_hashes": restored_snapshot,
                "hashes_match": hashes_match,
                "verification": verification,
            }
        )
        if not all_checks:
            raise DrillError("restored database did not satisfy every verification check")
        return report
    finally:
        cleaned = _cleanup(resources)
        report["temporary_resources_cleaned"] = cleaned
        if not cleaned:
            report["status"] = "FAIL"
        _write_report(report_path, report)
        if not cleaned:
            raise DrillError("one or more labeled temporary resources were not cleaned")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an isolated, non-production PostgreSQL backup/restore drill."
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument(
        "--migration",
        type=Path,
        default=Path("migrations/postgresql/0001_audit.sql"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("output/postgres-restore-drill/latest.json"),
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print the isolation plan without contacting Docker or changing resources.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    migration = args.migration.resolve()
    resources = DrillResources.create()
    if args.plan:
        print(json.dumps(build_plan(resources, args.image, migration), indent=2, sort_keys=True))
        return 0
    try:
        report = run_restore_drill(
            image=args.image,
            migration=migration,
            report_path=args.report.resolve(),
        )
    except DrillError as error:
        print(f"PostgreSQL restore drill: FAIL ({error})", file=sys.stderr)
        return 1
    print(f"PostgreSQL restore drill: {report['status']}")
    print(f"Report: {args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
