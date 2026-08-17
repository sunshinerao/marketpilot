import sqlite3
from pathlib import Path

import pytest

from marketpilot.cli import main
from marketpilot.services.repository import SQLiteAuditRepository
from marketpilot.services.stream_attribution_store import StreamAttributionStore


def initialize_full_database(database: Path) -> None:
    repository = SQLiteAuditRepository(database)
    stream = StreamAttributionStore(database)
    stream.close()
    repository.close()


def test_audit_integrity_report_is_clean_for_initialized_repository(tmp_path: Path) -> None:
    repository = SQLiteAuditRepository(tmp_path / "audit.sqlite3")
    try:
        report = repository.integrity_check()
    finally:
        repository.close()

    assert report.ok is True
    assert report.schema_version == "1"
    assert report.quick_check == ("ok",)
    assert report.foreign_key_violations == 0
    assert report.append_only_triggers_installed == 10
    assert report.append_only_triggers_expected == 10


def test_audit_check_cli_reports_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "audit.sqlite3"
    initialize_full_database(database)

    result = main(["audit-check", "--database", str(database)])

    assert result == 0
    captured = capsys.readouterr()
    assert "status=PASS" in captured.out
    assert "foreign_key_violations=0" in captured.out
    assert "append_only_triggers=18/18" in captured.out


def test_audit_check_cli_does_not_create_a_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "missing.sqlite3"

    result = main(["audit-check", "--database", str(database)])

    assert result == 2
    assert database.exists() is False
    assert "reason=NOT_FOUND" in capsys.readouterr().out


def test_audit_check_is_read_only_and_fails_when_trigger_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "tampered.sqlite3"
    initialize_full_database(database)
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER decision_runs_deny_update")
    connection.commit()
    connection.close()
    modified_before = database.stat().st_mtime_ns

    result = main(["audit-check", "--database", str(database)])

    assert result == 2
    assert database.stat().st_mtime_ns == modified_before
    assert "append_only_triggers=17/18" in capsys.readouterr().out
    verification = sqlite3.connect(database).execute(
        "SELECT count(*) FROM sqlite_master "
        "WHERE type = 'trigger' AND name = 'decision_runs_deny_update'"
    ).fetchone()
    assert verification == (0,)


def test_audit_check_rejects_counterfeit_trigger_with_expected_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "counterfeit.sqlite3"
    initialize_full_database(database)
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER decision_runs_deny_update")
    connection.execute(
        "CREATE TRIGGER decision_runs_deny_update "
        "AFTER INSERT ON decision_runs BEGIN SELECT 1; END"
    )
    connection.commit()
    connection.close()

    result = main(["audit-check", "--database", str(database)])

    assert result == 2
    assert "append_only_triggers=17/18" in capsys.readouterr().out


def test_audit_check_includes_stream_and_attribution_triggers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "full-runtime.sqlite3"
    initialize_full_database(database)
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER stream_deliveries_deny_update")
    connection.execute(
        "CREATE TRIGGER stream_deliveries_deny_update "
        "AFTER INSERT ON stream_deliveries BEGIN SELECT 1; END"
    )
    connection.commit()
    connection.close()

    result = main(["audit-check", "--database", str(database)])

    assert result == 2
    assert "append_only_triggers=17/18" in capsys.readouterr().out


def test_audit_check_rejects_complete_stream_schema_deletion(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "deleted-stream-schema.sqlite3"
    initialize_full_database(database)
    connection = sqlite3.connect(database)
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

    result = main(["audit-check", "--database", str(database)])

    assert result == 2
    assert "append_only_triggers=10/18" in capsys.readouterr().out
