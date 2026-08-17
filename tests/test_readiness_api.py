from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketpilot.domain.readiness import ReadinessManifest
from marketpilot.services.readiness import save_readiness_manifest
from marketpilot.services.readiness_router import create_readiness_router


def client(manifest: Path, ledger: Path) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_readiness_router(
            manifest_path=manifest,
            shadow_ledger_path=ledger,
            minimum_sessions=1,
            minimum_trading_dates=1,
        )
    )
    return TestClient(app)


def test_readiness_api_is_fail_closed_when_evidence_is_absent(tmp_path: Path) -> None:
    response = client(tmp_path / "missing.json", tmp_path / "missing.jsonl").get(
        "/v1/readiness/shadow-admission"
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "NOT_CONFIGURED",
        "evidence_complete": False,
        "shadow_admission_ready": False,
        "production_ready": False,
        "execution_enabled": False,
        "action": "NO_TRADE",
        "manual_webull_execution_only": True,
        "blockers": ["READINESS_EVIDENCE_MISSING_OR_INVALID"],
    }


def test_readiness_api_reports_unverified_template_as_blocked(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    save_readiness_manifest(
        manifest_path,
        ReadinessManifest.unverified_template(
            generated_at=datetime(2026, 8, 16, 12, tzinfo=UTC),
            environment="production",
        ),
    )
    response = client(manifest_path, tmp_path / "missing.jsonl").get(
        "/v1/readiness/shadow-admission"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "BLOCKED"
    assert payload["shadow_admission_ready"] is False
    assert payload["production_ready"] is False
    assert payload["execution_enabled"] is False
    assert payload["action"] == "NO_TRADE"
    assert "EVIDENCE_UNVERIFIED:WEBULL_ACCOUNT_ENTITLEMENT" in payload["blockers"]
    serialized = response.text
    assert "issuer" not in serialized
    assert "scope" not in serialized
