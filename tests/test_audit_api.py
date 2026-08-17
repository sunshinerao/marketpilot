from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketpilot.services.audit_router import create_audit_router
from marketpilot.services.repository import SQLiteAuditRepository
from marketpilot.services.schemas import DecisionRunOutput


def decision(run_id: str, *, minute: int) -> DecisionRunOutput:
    return DecisionRunOutput(
        run_id=run_id,
        run_mode="SCENARIO",
        model_id="model",
        model_version="model-v1",
        rules_version="rules-v1",
        code_version="code-v1",
        snapshot_id=f"sha256:{run_id}",
        data_as_of=datetime(2026, 8, 16, 12, minute, tzinfo=UTC),
        action="NO_TRADE",
        reasons=["EVENT_PENDING"],
        output={},
    )


def test_history_and_integrity_are_read_only_fail_closed_contracts() -> None:
    repository = SQLiteAuditRepository()
    repository.append_decision(decision("run-1", minute=1))
    repository.append_decision(decision("run-2", minute=2))
    app = FastAPI()
    app.include_router(create_audit_router(repository))
    client = TestClient(app)

    history = client.get("/v1/history/decisions?limit=1")
    integrity = client.get("/v1/audit/integrity")

    assert history.status_code == 200
    assert history.json()["scope"] == "LOCAL"
    assert history.json()["execution_enabled"] is False
    assert history.json()["action"] == "NO_TRADE"
    assert len(history.json()["decisions"]) == 1
    assert integrity.status_code == 200
    assert integrity.json()["status"] == "PASS"
    assert integrity.json()["foreign_key_violations"] == 0


def test_history_limit_is_bounded_at_http_and_repository_layers() -> None:
    repository = SQLiteAuditRepository()
    app = FastAPI()
    app.include_router(create_audit_router(repository))
    client = TestClient(app)

    assert client.get("/v1/history/decisions?limit=0").status_code == 422
    assert client.get("/v1/history/decisions?limit=1001").status_code == 422

    try:
        repository.decisions(limit=0)
    except ValueError as exc:
        assert "[1, 1000]" in str(exc)
    else:  # pragma: no cover - defensive assertion.
        raise AssertionError("repository accepted an unsafe limit")
