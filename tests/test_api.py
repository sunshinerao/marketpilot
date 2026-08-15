from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from marketpilot.services.api import (
    get_decision,
    health,
    market_state,
    model_health,
    models,
    run_decision,
)
from marketpilot.services.schemas import DecisionRunInput, GateInput


def test_health_and_model_registry_are_exposed() -> None:
    assert health()["status"] == "ok"
    registered = models()
    assert registered[0]["model_id"] == "strikepilot_spxw_0dte_ic"


def test_model_health_is_explicitly_not_calibrated() -> None:
    assert model_health()["status"] == "NOT_CALIBRATED"


def test_default_api_run_is_safe_no_trade_and_replayable() -> None:
    response = run_decision(DecisionRunInput(as_of=datetime(2026, 8, 17, 13, 45, tzinfo=UTC)))
    assert response.action == "NO_TRADE"
    assert "STALE_MARKET_DATA" in response.reasons
    assert get_decision(response.run_id) == response


def test_green_demo_run_returns_non_executable_baseline_strike_map() -> None:
    response = run_decision(
        DecisionRunInput(
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
    assert response.output["short_put"] == 7770
    assert response.output["short_call"] == 7845


def test_unknown_decision_returns_404() -> None:
    with pytest.raises(HTTPException) as error:
        get_decision("missing")
    assert error.value.status_code == 404


def test_market_state_remains_red_until_capability_probe() -> None:
    state = market_state()
    assert state["quality"] == "RED"
    assert state["execution_enabled"] is False
