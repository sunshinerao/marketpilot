from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from marketpilot.services import api
from marketpilot.services.operations import OperationsService


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(api, "operations", OperationsService())
    return TestClient(api.app, raise_server_exceptions=False)


def test_overview_is_live_server_derived_and_fail_closed(client: TestClient) -> None:
    response = client.get("/v1/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_mode"] == "LIVE"
    assert payload["code_version"] == "development-unpinned"
    assert payload["verification"] == "UNVERIFIED"
    assert payload["execution_enabled"] is False
    assert payload["action"] == "NO_TRADE"
    assert payload["risk_lock"]["status"] == "LOCKED"
    assert payload["replay"]["status"] == "DEMO_ONLY"


def test_unverified_live_overview_stays_no_trade_when_market_is_green(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "market_state",
        lambda: {"quality": "GREEN", "reason": "PROVIDER_SCHEMA_OBSERVED"},
    )

    response = client.get("/v1/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["market"]["status"] == "GREEN"
    assert payload["verification"] == "UNVERIFIED"
    assert payload["action"] == "NO_TRADE"
    assert payload["execution_enabled"] is False


def test_demo_scenarios_expose_deterministic_point_in_time_manifests(
    client: TestClient,
) -> None:
    first = client.get("/v1/demo/scenarios")
    second = client.get("/v1/demo/scenarios")

    assert first.status_code == 200
    assert first.json() == second.json()
    assert len(first.json()) == 2
    for scenario in first.json():
        assert scenario["run_mode"] == "SCENARIO"
        assert scenario["verification"] == "UNVERIFIED"
        assert scenario["execution_enabled"] is False
        assert scenario["action"] == "NO_TRADE"
        assert scenario["replay_manifest"]["manifest_hash"].startswith("sha256:")
        assert len(scenario["replay_manifest"]["entries"]) == 1


def test_decision_provenance_includes_code_version_in_snapshot_identity(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "_server_now",
        lambda: datetime(2026, 8, 17, 13, 45, tzinfo=UTC),
    )
    monkeypatch.setattr(api, "CODE_VERSION", "code-v1")
    first = client.post("/v1/decision/run", json={})
    monkeypatch.setattr(api, "CODE_VERSION", "code-v2")
    second = client.post("/v1/decision/run", json={})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["code_version"] == "code-v1"
    assert second.json()["code_version"] == "code-v2"
    assert first.json()["snapshot_id"] != second.json()["snapshot_id"]


def test_event_assessment_rejects_live_client_facts(client: TestClient) -> None:
    response = client.post(
        "/v1/events/assess",
        json={
            "run_mode": "LIVE",
            "as_of": "2026-08-17T13:01:00Z",
            "event": _locked_event(),
        },
    )

    assert response.status_code == 422
    assert "scenario-only" in response.text


def test_scenario_event_assessment_never_creates_a_trade(client: TestClient) -> None:
    response = client.post(
        "/v1/events/assess",
        json={
            "run_mode": "SCENARIO",
            "as_of": "2026-08-17T13:01:00Z",
            "event": _locked_event(),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["assessment"]["state"] == "LOCKED"
    assert payload["action"] == "NO_TRADE"
    assert payload["verification"] == "UNVERIFIED"
    assert payload["execution_enabled"] is False


def test_alert_feedback_updates_demo_state_but_never_enables_execution(
    client: TestClient,
) -> None:
    alert_response = client.get("/v1/alerts")
    assert alert_response.status_code == 200
    alert_payload = alert_response.json()
    assert alert_payload["run_mode"] == "SCENARIO"
    assert alert_payload["verification"] == "UNVERIFIED"
    alert_id = alert_payload["alerts"][0]["alert_id"]

    feedback_response = client.post(
        f"/v1/alerts/{alert_id}/feedback",
        json={
            "kind": "ACKNOWLEDGED",
            "actor": "demo-operator",
            "recorded_at": "2026-08-17T13:01:30Z",
            "note": "Local operations demo only",
        },
    )

    assert feedback_response.status_code == 200
    feedback = feedback_response.json()
    assert feedback["feedback"]["alert_id"] == alert_id
    assert feedback["alert"]["status"] == "ACKNOWLEDGED"
    assert feedback["execution_enabled"] is False
    assert client.get("/v1/alerts").json()["alerts"][0]["status"] == "ACKNOWLEDGED"


def test_unknown_alert_feedback_returns_404(client: TestClient) -> None:
    response = client.post(
        "/v1/alerts/missing/feedback",
        json={
            "kind": "DISMISSED",
            "actor": "demo-operator",
            "recorded_at": "2026-08-17T13:01:30Z",
        },
    )

    assert response.status_code == 404


def _locked_event() -> dict[str, object]:
    return {
        "event_id": "api-demo-shock",
        "kind": "MARKET_SHOCK",
        "severity": "P0",
        "source_published_at": "2026-08-17T13:00:00Z",
        "first_seen_at": "2026-08-17T13:00:05Z",
        "corroborating_sources": 1,
        "cross_asset_confirmed": False,
    }
