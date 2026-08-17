from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketpilot.services.economics_router import create_economics_router


def client() -> TestClient:
    app = FastAPI()
    app.include_router(create_economics_router())
    return TestClient(app)


def payload() -> dict[str, object]:
    return {
        "run_mode": "SCENARIO",
        "scope": "LOCAL",
        "valued_at": "2026-08-16T14:30:01Z",
        "quotes": [
            {
                "leg_id": "short-call",
                "quantity": -1,
                "multiplier": 100,
                "bid": 5.0,
                "ask": 5.2,
                "bid_size": 10,
                "ask_size": 10,
                "quoted_at": "2026-08-16T14:30:00Z",
            },
            {
                "leg_id": "long-call",
                "quantity": 1,
                "multiplier": 100,
                "bid": 2.0,
                "ask": 2.2,
                "bid_size": 10,
                "ask_size": 10,
                "quoted_at": "2026-08-16T14:30:00Z",
            },
        ],
        "assumptions": {
            "max_quote_age_seconds": 2,
            "fee_per_contract": 0.5,
            "slippage_per_contract": 0.05,
            "max_size_participation": 0.5,
        },
        "scenarios": [
            {"name": "base", "probability": 0.95, "conservative_pnl": 30},
            {"name": "tail", "probability": 0.05, "conservative_pnl": -400},
        ],
        "risk": {
            "max_loss": 400,
            "risk_budget": 500,
            "cvar_budget": 400,
            "cvar_confidence": 0.95,
        },
    }


def test_eligible_economic_subgate_is_review_only_wait() -> None:
    response = client().post("/v1/scenario/economics/assess", json=payload())

    assert response.status_code == 200
    body = response.json()
    assert body["run_mode"] == "SCENARIO"
    assert body["scope"] == "LOCAL"
    assert body["verification"] == "UNVERIFIED"
    assert body["execution_enabled"] is False
    assert body["manual_execution_only"] is True
    assert body["action"] == "WAIT"
    assert body["risk"]["risk_gate_cleared"] is True
    assert body["opening_value"]["net_cashflow"] == 269.0


def test_unusable_quote_suppresses_exact_legs_and_credit() -> None:
    request = payload()
    quotes = request["quotes"]
    assert isinstance(quotes, list)
    quotes[0]["quoted_at"] = "2026-08-16T14:29:00Z"

    response = client().post("/v1/scenario/economics/assess", json=request)

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "NO_TRADE"
    assert body["risk"]["reason"] == "OPTION_CHAIN_UNUSABLE"
    assert body["opening_value"]["is_executable"] is False
    assert body["opening_value"]["legs"] == []
    assert body["opening_value"]["gross_cashflow"] is None
    assert body["opening_value"]["net_cashflow"] is None


def test_live_missing_scope_and_naive_time_are_rejected() -> None:
    api = client()
    live = payload()
    live["run_mode"] = "LIVE"
    missing_scope = payload()
    missing_scope.pop("scope")
    naive = payload()
    naive["valued_at"] = "2026-08-16T14:30:01"

    assert api.post("/v1/scenario/economics/assess", json=live).status_code == 422
    assert api.post("/v1/scenario/economics/assess", json=missing_scope).status_code == 422
    response = api.post("/v1/scenario/economics/assess", json=naive)
    assert response.status_code == 422
    assert "timezone-aware" in response.text


def test_bad_probability_sum_and_unbounded_loss_fail_closed() -> None:
    api = client()
    bad_probability = payload()
    scenarios = bad_probability["scenarios"]
    assert isinstance(scenarios, list)
    scenarios[0]["probability"] = 0.9

    assert api.post(
        "/v1/scenario/economics/assess", json=bad_probability
    ).status_code == 422

    unbounded = payload()
    risk = unbounded["risk"]
    assert isinstance(risk, dict)
    risk["max_loss"] = None
    response = api.post("/v1/scenario/economics/assess", json=unbounded)
    assert response.status_code == 200
    assert response.json()["action"] == "NO_TRADE"
    assert response.json()["risk"]["reason"] == "RISK_BUDGET_EXCEEDED"
