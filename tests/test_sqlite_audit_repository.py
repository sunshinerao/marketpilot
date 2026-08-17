from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marketpilot.domain.alerts import FeedbackKind
from marketpilot.domain.decision import DecisionAction, NoTradeReason
from marketpilot.services import api
from marketpilot.services.demo import demo_scenario_artifacts
from marketpilot.services.operations import OperationsService
from marketpilot.services.repository import ImmutableAuditConflict, SQLiteAuditRepository
from marketpilot.services.schemas import DecisionRunMode, DecisionRunOutput
from marketpilot.services.state import DecisionStore


def decision(run_id: str = "run-a") -> DecisionRunOutput:
    return DecisionRunOutput(
        run_id=run_id,
        run_mode=DecisionRunMode.LIVE,
        model_id="strikepilot_spxw_0dte_ic",
        model_version="0.1.0-baseline",
        rules_version="rules-v1",
        code_version="test-code-v1",
        snapshot_id="sha256:test",
        data_as_of=datetime(2026, 8, 17, 13, 45, tzinfo=UTC),
        action=DecisionAction.NO_TRADE,
        reasons=[NoTradeReason.STALE_MARKET_DATA],
        output={},
    )


def test_decision_is_durable_idempotent_and_immutable(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    first = SQLiteAuditRepository(path)
    first.append_decision(decision())
    first.append_decision(decision())
    with pytest.raises(ImmutableAuditConflict):
        first.append_decision(decision().model_copy(update={"code_version": "changed"}))
    first.close()

    reopened = SQLiteAuditRepository(path)
    assert reopened.schema_version() == "1"
    assert reopened.get_decision("run-a") == decision()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        reopened._connection.execute(  # noqa: SLF001 - verifies the database invariant.
            "UPDATE decision_runs SET payload_json = '{}' WHERE run_id = 'run-a'"
        )
    reopened.close()


def test_concurrent_idempotent_decision_appends_are_serialized(tmp_path: Path) -> None:
    repository = SQLiteAuditRepository(tmp_path / "audit.sqlite3")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(repository.append_decision, [decision()] * 32))

    assert repository.get_decision("run-a") == decision()
    repository.close()


def test_alert_feedback_survives_service_restart(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    first_repository = SQLiteAuditRepository(path)
    first_operations = OperationsService(first_repository)
    alert_id = first_operations.alerts()[0].alert_id
    feedback, alert = first_operations.record_feedback(
        alert_id=alert_id,
        kind=FeedbackKind.ACKNOWLEDGED,
        actor="operator",
        recorded_at=datetime(2026, 8, 17, 13, 1, tzinfo=UTC),
        note="durability check",
    )
    assert alert.status == "ACKNOWLEDGED"
    first_repository.close()

    reopened = SQLiteAuditRepository(path)
    restarted_operations = OperationsService(reopened)
    assert restarted_operations.alerts()[0].status == "ACKNOWLEDGED"
    assert restarted_operations.feedback(alert_id) == (feedback,)
    reopened.close()


def test_pit_audit_stores_only_provenance_metadata(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite3"
    artifact = demo_scenario_artifacts()[0]
    repository = SQLiteAuditRepository(path)
    repository.append_point_in_time_record(artifact.record)
    repository.append_replay_manifest(artifact.manifest)
    repository.close()

    reopened = SQLiteAuditRepository(path)
    records = reopened.point_in_time_records()
    manifests = reopened.replay_manifests()
    assert records[0].content_hash == artifact.record.content_hash
    assert manifests[0].manifest_hash == artifact.manifest.manifest_hash
    with reopened._lock:  # noqa: SLF001 - verify storage, not the public projection.
        payload = reopened._connection.execute(  # noqa: SLF001
            "SELECT payload_json FROM point_in_time_records"
        ).fetchone()[0]
    assert "canonical_content" not in payload
    assert "cross_asset_confirmed" not in payload
    reopened.close()


def test_decision_and_feedback_are_readable_through_api_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "audit.sqlite3"
    first_repository = SQLiteAuditRepository(path)
    monkeypatch.setattr(api, "decisions", DecisionStore(first_repository))
    monkeypatch.setattr(api, "operations", OperationsService(first_repository))
    client = TestClient(api.app, raise_server_exceptions=False)

    decision_response = client.post(
        "/v1/decision/run",
        json={},
    )
    run_id = decision_response.json()["run_id"]
    alert_id = client.get("/v1/alerts").json()["alerts"][0]["alert_id"]
    feedback_response = client.post(
        f"/v1/alerts/{alert_id}/feedback",
        json={
            "kind": "ACKNOWLEDGED",
            "actor": "operator",
            "recorded_at": "2026-08-17T13:02:00Z",
        },
    )
    assert feedback_response.status_code == 200
    first_repository.close()

    reopened = SQLiteAuditRepository(path)
    monkeypatch.setattr(api, "decisions", DecisionStore(reopened))
    monkeypatch.setattr(api, "operations", OperationsService(reopened))
    restarted_client = TestClient(api.app, raise_server_exceptions=False)

    assert restarted_client.get(f"/v1/decisions/{run_id}").status_code == 200
    feedback_history = restarted_client.get(f"/v1/alerts/{alert_id}/feedback")
    assert feedback_history.status_code == 200
    assert feedback_history.json()["feedback"][0]["kind"] == "ACKNOWLEDGED"
    assert restarted_client.get("/v1/alerts").json()["alerts"][0]["status"] == "ACKNOWLEDGED"
    reopened.close()
