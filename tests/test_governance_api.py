from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketpilot.domain.decision import DecisionAction
from marketpilot.domain.governance import ModelVersion
from marketpilot.services.governance_router import create_governance_router
from marketpilot.services.governance_service import BASELINE_MODEL_ID, GovernanceService
from marketpilot.validation.metrics import ValidationResult

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def client_for(service: GovernanceService) -> TestClient:
    app = FastAPI()
    app.include_router(create_governance_router(service))
    return TestClient(app)


def approval_payload(target: str, *, source: str | None, evidence: str) -> dict[str, object]:
    return {
        "run_mode": "SCENARIO",
        "scope": "LOCAL",
        "source_version": source,
        "target_version": target,
        "approved_by": "local-risk-reviewer",
        "approved_at": NOW.isoformat(),
        "evidence_hash": evidence,
        "note": "local scenario approval only",
    }


def challenger(version: str, *, parent: str | None = None) -> ModelVersion:
    return ModelVersion(
        model_id=BASELINE_MODEL_ID,
        version=version,
        artifact_hash=f"sha256:artifact-{version}",
        data_manifest_hash=f"sha256:data-{version}",
        trained_at=NOW,
        validation_report_hash=f"sha256:validation-{version}",
        parent_version=parent,
    )


def test_default_baseline_is_not_calibrated_and_has_no_champion() -> None:
    client = client_for(GovernanceService())
    versions = client.get(f"/v1/governance/models/{BASELINE_MODEL_ID}/versions")
    champion = client.get(f"/v1/governance/models/{BASELINE_MODEL_ID}/champion")
    validation = client.get(f"/v1/governance/models/{BASELINE_MODEL_ID}/validation")

    assert versions.status_code == 200
    assert versions.json()["versions"][0]["calibration_status"] == "NOT_CALIBRATED"
    assert versions.json()["live_enabled"] is False
    assert champion.status_code == 409
    assert validation.json()["calibration_status"] == "NOT_CALIBRATED"
    assert validation.json()["slices"] == []
    assert validation.json()["live_eligible"] is False


def test_demo_evidence_cannot_promote_unvalidated_baseline() -> None:
    client = client_for(GovernanceService())
    response = client.post(
        f"/v1/governance/models/{BASELINE_MODEL_ID}/promotions",
        json=approval_payload(
            "0.1.0-baseline",
            source=None,
            evidence="sha256:demo-evidence",
        ),
    )
    assert response.status_code == 409
    assert "no frozen validation report" in response.json()["detail"]


def test_contract_rejects_live_or_non_scenario_approval_modes() -> None:
    client = client_for(GovernanceService())
    payload = approval_payload("0.1.0-baseline", source=None, evidence="sha256:demo")
    payload["scope"] = "LIVE"
    assert (
        client.post(
            f"/v1/governance/models/{BASELINE_MODEL_ID}/promotions", json=payload
        ).status_code
        == 422
    )


def test_contract_rejects_naive_time_blank_identity_and_extra_fields() -> None:
    client = client_for(GovernanceService())
    endpoint = f"/v1/governance/models/{BASELINE_MODEL_ID}/promotions"
    payload = approval_payload("0.1.0-baseline", source=None, evidence="sha256:demo")
    payload["approved_at"] = "2026-08-17T12:00:00"
    assert client.post(endpoint, json=payload).status_code == 422

    payload = approval_payload(" ", source=None, evidence="sha256:demo")
    assert client.post(endpoint, json=payload).status_code == 422

    payload = approval_payload("0.1.0-baseline", source=None, evidence="sha256:demo")
    payload["live_override"] = True
    assert client.post(endpoint, json=payload).status_code == 422
    payload["scope"] = "LOCAL"
    payload["run_mode"] = "LIVE"
    assert (
        client.post(
            f"/v1/governance/models/{BASELINE_MODEL_ID}/promotions", json=payload
        ).status_code
        == 422
    )


def test_local_promotion_freezes_session_and_local_rollback_is_auditable() -> None:
    service = GovernanceService()
    first = challenger("1.0.0")
    service.register_local_challenger(first)
    client = client_for(service)
    promoted = client.post(
        f"/v1/governance/models/{BASELINE_MODEL_ID}/promotions",
        json=approval_payload(
            first.version,
            source=None,
            evidence=first.validation_report_hash or "",
        ),
    )
    assert promoted.status_code == 200
    assert promoted.json()["live_enabled"] is False
    assert promoted.json()["champion"]["champion"]["live_eligible"] is False

    session = client.get(
        f"/v1/governance/models/{BASELINE_MODEL_ID}/champion",
        params={"session_id": "SCENARIO:governance-test-2026-08-17"},
    )
    assert session.json()["champion"]["version"] == first.version
    assert session.json()["frozen_for_session"] is True

    rolled_back = client.post(
        f"/v1/governance/models/{BASELINE_MODEL_ID}/rollbacks",
        json=approval_payload(
            "0.1.0-baseline",
            source=first.version,
            evidence="sha256:local-incident-review",
        ),
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["action"] == "ROLLBACK"
    assert rolled_back.json()["champion"]["champion"]["version"] == "0.1.0-baseline"
    assert (
        client.get(
            f"/v1/governance/models/{BASELINE_MODEL_ID}/champion",
            params={"session_id": "SCENARIO:governance-test-2026-08-17"},
        ).json()["champion"]["version"]
        == first.version
    )


def test_public_governance_endpoint_cannot_freeze_live_session() -> None:
    service = GovernanceService()
    first = challenger("1.0.0")
    service.register_local_challenger(first)
    client = client_for(service)
    assert client.post(
        f"/v1/governance/models/{BASELINE_MODEL_ID}/promotions",
        json=approval_payload(
            first.version,
            source=None,
            evidence=first.validation_report_hash or "",
        ),
    ).status_code == 200

    response = client.get(
        f"/v1/governance/models/{BASELINE_MODEL_ID}/champion",
        params={"session_id": "LIVE:XNYS:2026-08-17"},
    )

    assert response.status_code == 409
    assert "restricted to SCENARIO" in response.json()["detail"]


def test_validation_summary_exposes_strata_without_live_conclusion() -> None:
    service = GovernanceService()
    service.record_local_validation(
        BASELINE_MODEL_ID,
        report_hash="sha256:local-report",
        results=(
            ValidationResult(
                sample_id="event-day",
                event_type="P1",
                regime="HIGH_VOL",
                action=DecisionAction.NO_TRADE,
                counterfactual_pnl=-50.0,
                realized_pnl=None,
                metrics={"coverage": 1.0},
            ),
        ),
    )
    response = client_for(service).get(f"/v1/governance/models/{BASELINE_MODEL_ID}/validation")
    body = response.json()
    assert response.status_code == 200
    assert body["calibration_status"] == "LOCAL_VALIDATION_AVAILABLE"
    assert body["slices"][0]["strata"] == [["event_type", "P1"], ["regime", "HIGH_VOL"]]
    assert body["slices"][0]["no_trade_effect"]["no_trade_count"] == 1
    assert body["conclusion"] == "NO_AUTOMATIC_PERFORMANCE_CONCLUSION"
    assert body["live_eligible"] is False


def test_visible_local_challenger_registration_and_promotion_loop() -> None:
    client = client_for(GovernanceService())
    registered = client.post(
        f"/v1/governance/models/{BASELINE_MODEL_ID}/challengers",
        json={
            "run_mode": "SCENARIO",
            "scope": "LOCAL",
            "version": "1.2.0-local",
            "artifact_hash": "sha256:artifact-1.2.0",
            "data_manifest_hash": "sha256:data-1.2.0",
            "trained_at": NOW.isoformat(),
            "validation_report_hash": "sha256:validation-1.2.0",
        },
    )
    assert registered.status_code == 200, registered.text
    assert registered.json()["action"] == "NO_TRADE"
    assert registered.json()["execution_enabled"] is False
    assert registered.json()["live_eligible"] is False

    promoted = client.post(
        f"/v1/governance/models/{BASELINE_MODEL_ID}/promotions",
        json=approval_payload(
            "1.2.0-local",
            source=None,
            evidence="sha256:validation-1.2.0",
        ),
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["champion"]["champion"]["version"] == "1.2.0-local"
