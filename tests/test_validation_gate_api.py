from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketpilot.services.validation_gate_router import create_validation_gate_router
from marketpilot.validation.promotion_gate import load_promotion_criteria


def client() -> TestClient:
    app = FastAPI()
    app.include_router(
        create_validation_gate_router(
            load_promotion_criteria("config/promotion-criteria-v1.toml")
        )
    )
    return TestClient(app)


def payload() -> dict[str, object]:
    slices = [
        {
            "event_type": event,
            "regime": regime,
            "sample_count": 120,
            "expiry_cross_rate": 0.04,
            "touch_rate": 0.09,
            "cvar": 450,
            "maximum_drawdown": 900,
            "no_trade_pnl_difference": 10,
        }
        for event, regime in (
            ("NORMAL", "LOW_VOL"),
            ("NORMAL", "HIGH_VOL"),
            ("P1", "HIGH_VOL"),
            ("P0", "HIGH_VOL"),
        )
    ]
    return {
        "run_mode": "SCENARIO",
        "scope": "LOCAL",
        "data_manifest_hash": "sha256:data",
        "holdout_manifest_hash": "sha256:holdout",
        "holdout_frozen_at": "2026-08-17T00:00:00Z",
        "evaluated_at": "2026-08-18T00:00:00Z",
        "slices": slices,
    }


def test_criteria_and_passing_report_never_enable_live_or_enter() -> None:
    api = client()
    criteria = api.get("/v1/validation/promotion-criteria")
    report = api.post("/v1/validation/promotion-gate", json=payload())

    assert criteria.status_code == 200
    assert criteria.json()["criteria_hash"].startswith("sha256:")
    assert criteria.json()["live_eligible"] is False
    assert report.status_code == 200
    assert report.json()["passed"] is True
    assert report.json()["local_promotion_evidence_available"] is True
    assert report.json()["action"] == "NO_TRADE"
    assert report.json()["execution_enabled"] is False
    assert report.json()["live_eligible"] is False


def test_live_missing_slice_and_naive_timestamps_fail_closed() -> None:
    api = client()
    live = payload()
    live["run_mode"] = "LIVE"
    assert api.post("/v1/validation/promotion-gate", json=live).status_code == 422

    naive = payload()
    naive["evaluated_at"] = "2026-08-18T00:00:00"
    assert api.post("/v1/validation/promotion-gate", json=naive).status_code == 422

    missing = payload()
    slices = missing["slices"]
    assert isinstance(slices, list)
    slices.pop()
    report = api.post("/v1/validation/promotion-gate", json=missing)
    assert report.status_code == 200
    assert report.json()["passed"] is False
    assert report.json()["action"] == "NO_TRADE"
    assert "P0/HIGH_VOL:MISSING_SLICE" in report.json()["failures"]
