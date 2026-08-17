from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from marketpilot.domain.capabilities import CapabilityReport
from marketpilot.domain.governance import ApprovalAction, GovernanceApproval, ModelVersion
from marketpilot.domain.market import DataQuality
from marketpilot.governance.registry import FrozenChampionRegistry
from marketpilot.services import api
from marketpilot.services.api import (
    get_decision,
    health,
    market_state,
    model_health,
    models,
    run_decision,
    webull_capabilities,
)
from marketpilot.services.capability_store import CapabilityReportStore
from marketpilot.services.schemas import DecisionRunInput, GateInput

client = TestClient(api.app, raise_server_exceptions=False)


def _promote(
    registry: FrozenChampionRegistry,
    model: ModelVersion,
    *,
    source: str | None,
    minute: int,
) -> None:
    registry.register_challenger(model)
    registry.promote(
        model.model_id,
        model.version,
        GovernanceApproval.create(
            action=ApprovalAction.PROMOTE,
            model_id=model.model_id,
            source_version=source,
            target_version=model.version,
            approved_by="api-governance-test",
            approved_at=model.trained_at + timedelta(minutes=minute),
            evidence_hash=model.validation_report_hash or "",
            note=f"test promotion {model.version}",
        ),
    )


def _model_version(version: str, artifact_hash: str, *, parent: str | None = None) -> ModelVersion:
    return ModelVersion(
        model_id=api.strikepilot_model.descriptor.model_id,
        version=version,
        artifact_hash=artifact_hash,
        data_manifest_hash=f"sha256:data-{version}",
        trained_at=datetime(2026, 8, 17, 12, tzinfo=UTC),
        validation_report_hash=f"sha256:validation-{version}",
        parent_version=parent,
    )


def test_health_and_model_registry_are_exposed() -> None:
    assert health()["status"] == "ok"
    registered = models()
    assert registered[0]["model_id"] == "strikepilot_spxw_0dte_ic"


def test_integrated_safety_apis_are_registered() -> None:
    paths = api.app.openapi()["paths"]
    expected = {
        "/v1/scenario/session-quality/equity-session",
        "/v1/scenario/session-quality/globex-session",
        "/v1/scenario/session-quality/quote-quality",
        "/v1/scenario/economics/assess",
        "/v1/alerts/stream",
        "/v1/alerts/stream/deliveries",
        "/v1/attribution/signals",
        "/v1/attribution/tasks",
        "/v1/history/decisions",
        "/v1/history/replay-manifests",
        "/v1/audit/integrity",
        "/v1/validation/promotion-criteria",
        "/v1/validation/promotion-gate",
        "/v1/scenario/collector/run",
        "/v1/scenario/alert-delivery/run",
    }
    assert expected.issubset(paths)


def test_model_health_is_explicitly_not_calibrated() -> None:
    assert model_health()["status"] == "NOT_CALIBRATED"


def test_runtime_rules_are_loaded_and_bound_to_model_and_snapshot() -> None:
    assert api.rules_config.version == "rules-v1"
    assert api.rules_config.strike_increment == 5
    assert api.rules_config.wing_width == 5

    first = run_decision(
        DecisionRunInput(
            run_mode="SCENARIO",
            scenario_session_id="rules-binding",
            as_of=datetime(2026, 8, 17, 13, 45, tzinfo=UTC),
            values={"center": 7812.4, "up_tail": 28.6, "down_tail": 34.2},
            gates=GateInput(
                data_quality="GREEN",
                event_cleared=True,
                option_chain_usable=True,
                edge_ok=True,
            ),
        )
    )
    assert first.rules_version == api.rules_config.version
    assert first.output["short_put"] % api.rules_config.strike_increment == 0


def test_default_api_run_is_safe_no_trade_and_replayable() -> None:
    response = run_decision(DecisionRunInput())
    assert response.action == "NO_TRADE"
    assert "STALE_MARKET_DATA" in response.reasons
    assert "MODEL_VERSION_NOT_LOADED" in response.reasons
    assert get_decision(response.run_id) == response


def test_green_demo_run_returns_non_executable_baseline_strike_map() -> None:
    response = run_decision(
        DecisionRunInput(
            run_mode="SCENARIO",
            scenario_session_id="green-demo",
            as_of=datetime(2026, 8, 17, 13, 45, tzinfo=UTC),
            values={"center": 7812.4, "up_tail": 28.6, "down_tail": 34.2, "joint_buffer": 3.5},
            gates=GateInput(
                data_quality="GREEN",
                event_cleared=True,
                option_chain_usable=True,
                edge_ok=True,
            ),
        )
    )
    assert response.action == "WAIT"
    assert response.run_mode == "SCENARIO"
    assert response.execution_enabled is False
    assert response.output["short_put"] == 7770
    assert response.output["short_call"] == 7845


def test_http_rejects_naive_as_of_instead_of_returning_500() -> None:
    response = client.post(
        "/v1/decision/run",
        json={
            "run_mode": "SCENARIO",
            "scenario_session_id": "naive-time",
            "as_of": "2026-08-17T13:45:00",
            "gates": {},
        },
    )

    assert response.status_code == 422
    assert "as_of must be timezone-aware" in response.text


def test_http_live_run_rejects_client_reported_gates() -> None:
    response = client.post(
        "/v1/decision/run",
        json={
            "as_of": "2026-08-17T13:45:00Z",
            "values": {"center": 7812.4, "up_tail": 28.6, "down_tail": 34.2},
            "gates": {
                "data_quality": "GREEN",
                "event_cleared": True,
                "option_chain_usable": True,
                "edge_ok": True,
            },
        },
    )

    assert response.status_code == 422
    assert "gates are server-derived for LIVE runs" in response.text


def test_http_live_run_rejects_client_market_values() -> None:
    response = client.post(
        "/v1/decision/run",
        json={
            "as_of": "2026-08-17T13:45:00Z",
            "values": {"center": 7812.4, "up_tail": 28.6, "down_tail": 34.2},
        },
    )

    assert response.status_code == 422
    assert "values are server-derived for LIVE runs" in response.text


def test_http_live_run_rejects_client_as_of_and_session_identity() -> None:
    timestamp = client.post(
        "/v1/decision/run",
        json={"as_of": "2099-01-01T00:00:00Z"},
    )
    session = client.post(
        "/v1/decision/run",
        json={"scenario_session_id": "forged-live-session"},
    )

    assert timestamp.status_code == 422
    assert "as_of is server-derived for LIVE runs" in timestamp.text
    assert session.status_code == 422
    assert "scenario_session_id is forbidden for LIVE runs" in session.text


def test_live_time_and_xnys_session_are_derived_only_from_server_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = FrozenChampionRegistry()
    loaded = api.strikepilot_model.descriptor
    _promote(
        registry,
        _model_version(loaded.version, loaded.artifact_hash),
        source=None,
        minute=1,
    )
    server_time = datetime(2026, 8, 17, 3, 30, tzinfo=UTC)
    monkeypatch.setattr(api, "runtime_persistence", SimpleNamespace(governance=registry))
    monkeypatch.setattr(api, "_server_now", lambda: server_time)

    response = client.post("/v1/decision/run", json={})

    assert response.status_code == 200
    assert response.json()["data_as_of"] == "2026-08-17T03:30:00Z"
    assert response.json()["governance_session_id"] == "LIVE:XNYS:2026-08-16"
    assert response.json()["model_artifact_hash"] == loaded.artifact_hash
    assert response.json()["governed_model_version"] == loaded.version
    assert response.json()["governed_model_artifact_hash"] == loaded.artifact_hash


def test_decision_path_freezes_scenario_champion_across_later_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = FrozenChampionRegistry()
    loaded = api.strikepilot_model.descriptor
    baseline = _model_version(loaded.version, loaded.artifact_hash)
    _promote(registry, baseline, source=None, minute=1)
    monkeypatch.setattr(api, "runtime_persistence", SimpleNamespace(governance=registry))
    request = {
        "run_mode": "SCENARIO",
        "scenario_session_id": "frozen-review",
        "as_of": "2026-08-17T13:45:00Z",
        "values": {"center": 7812.4, "up_tail": 28.6, "down_tail": 34.2},
        "gates": {
            "data_quality": "GREEN",
            "event_cleared": True,
            "option_chain_usable": True,
            "edge_ok": True,
        },
    }
    first = client.post("/v1/decision/run", json=request)
    challenger = _model_version(
        "1.1.0",
        "sha256:challenger-not-loaded",
        parent=baseline.version,
    )
    _promote(registry, challenger, source=baseline.version, minute=2)
    same_session = client.post("/v1/decision/run", json=request)
    new_session = client.post(
        "/v1/decision/run",
        json={**request, "scenario_session_id": "after-promotion"},
    )

    assert first.json()["action"] == "WAIT"
    assert same_session.json()["action"] == "WAIT"
    assert first.json()["governance_session_id"] == "SCENARIO:frozen-review"
    assert "MODEL_VERSION_NOT_LOADED" not in same_session.json()["reasons"]
    assert new_session.json()["action"] == "NO_TRADE"
    assert new_session.json()["reasons"] == ["MODEL_VERSION_NOT_LOADED"]


def test_http_same_version_wrong_artifact_hash_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = FrozenChampionRegistry()
    loaded = api.strikepilot_model.descriptor
    _promote(
        registry,
        _model_version(loaded.version, "sha256:wrong-runtime-artifact"),
        source=None,
        minute=1,
    )
    monkeypatch.setattr(api, "runtime_persistence", SimpleNamespace(governance=registry))

    response = client.post(
        "/v1/decision/run",
        json={
            "run_mode": "SCENARIO",
            "scenario_session_id": "artifact-mismatch",
            "as_of": "2026-08-17T13:45:00Z",
            "values": {"center": 7812.4, "up_tail": 28.6, "down_tail": 34.2},
            "gates": {
                "data_quality": "GREEN",
                "event_cleared": True,
                "option_chain_usable": True,
                "edge_ok": True,
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["model_version"] == loaded.version
    assert response.json()["action"] == "NO_TRADE"
    assert response.json()["reasons"] == ["MODEL_VERSION_NOT_LOADED"]
    assert response.json()["output"] == {}


def test_http_amber_scenario_fails_closed() -> None:
    response = client.post(
        "/v1/decision/run",
        json={
            "run_mode": "SCENARIO",
            "scenario_session_id": "amber-scenario",
            "as_of": "2026-08-17T13:45:00Z",
            "values": {"center": 7812.4, "up_tail": 28.6, "down_tail": 34.2},
            "gates": {
                "data_quality": "AMBER",
                "event_cleared": True,
                "option_chain_usable": True,
                "edge_ok": True,
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["action"] == "NO_TRADE"
    assert response.json()["run_mode"] == "SCENARIO"
    assert response.json()["execution_enabled"] is False
    assert "STALE_MARKET_DATA" in response.json()["reasons"]


def test_unknown_decision_returns_404() -> None:
    with pytest.raises(HTTPException) as error:
        get_decision("missing")
    assert error.value.status_code == 404


def test_market_state_remains_red_until_capability_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "capability_reports", CapabilityReportStore(tmp_path))
    state = market_state()
    assert state["quality"] == "RED"
    assert state["execution_enabled"] is False


def test_webull_capability_endpoint_is_explicit_before_first_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "capability_reports", CapabilityReportStore(tmp_path))
    response = webull_capabilities()
    assert response["provider"] == "webull"
    assert response["status"] == "NOT_RUN"


def test_corrupt_capability_report_keeps_http_api_red_and_live_no_trade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_dir = tmp_path / "webull"
    provider_dir.mkdir()
    (provider_dir / "latest.json").write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(api, "capability_reports", CapabilityReportStore(tmp_path))

    state = client.get("/v1/market/state")
    capability = client.get("/v1/providers/webull/capabilities")
    decision = client.post(
        "/v1/decision/run", json={}
    )

    assert state.status_code == 200
    assert state.json()["quality"] == "RED"
    assert capability.status_code == 200
    assert capability.json()["status"] == "NOT_RUN"
    assert decision.status_code == 200
    assert decision.json()["action"] == "NO_TRADE"


def test_schema_only_green_probe_is_effectively_amber_and_live_stays_no_trade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CapabilityReportStore(tmp_path)
    store.save(
        CapabilityReport(
            probed_at=datetime(2026, 8, 17, 13, 0, tzinfo=UTC),
            sdk_version="2.0.14",
            environment="us",
            configured=True,
            quality=DataQuality.GREEN,
            production_ready=False,
            results=(),
        )
    )
    monkeypatch.setattr(api, "capability_reports", store)

    state = market_state()
    decision = client.post(
        "/v1/decision/run",
        json={},
    )

    assert state["quality"] == "AMBER"
    assert state["reason"] == "WEBULL_SCHEMA_OBSERVED_NOT_VERIFIED"
    assert state["production_ready"] is False
    assert decision.status_code == 200
    assert decision.json()["action"] == "NO_TRADE"
    assert "STALE_MARKET_DATA" in decision.json()["reasons"]
