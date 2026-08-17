from __future__ import annotations

from datetime import UTC, datetime

import pytest

from marketpilot.domain.decision import DecisionAction, NoTradeReason
from marketpilot.validation.execution_economics import ExecutableValue
from marketpilot.validation.risk_economics import (
    ConservativeRiskContract,
    PnlScenario,
    assess_entry_risk,
)

NOW = datetime(2026, 8, 14, 14, 30, tzinfo=UTC)
EXECUTABLE = ExecutableValue(NOW, True, (), net_cashflow=100.0, net_credit=100.0)


def scenarios() -> tuple[PnlScenario, ...]:
    return (
        PnlScenario("tail", 0.05, -400.0),
        PnlScenario("down", 0.15, -100.0),
        PnlScenario("base", 0.60, 80.0),
        PnlScenario("up", 0.20, 150.0),
    )


def test_probability_weighted_ev_cvar_and_max_loss_contract() -> None:
    result = assess_entry_risk(
        EXECUTABLE,
        scenarios(),
        ConservativeRiskContract(
            max_loss=500.0,
            risk_budget=500.0,
            cvar_budget=400.0,
            cvar_confidence=0.95,
        ),
    )

    assert result.scenario_expected_pnl == pytest.approx(43.0)
    assert result.scenario_max_loss == 400.0
    assert result.tail_loss_cvar == pytest.approx(400.0)
    assert result.max_loss == 500.0
    assert result.entry_eligible is True
    assert result.action is DecisionAction.WAIT


@pytest.mark.parametrize(
    "contract",
    [
        ConservativeRiskContract(None, 500.0, 500.0),
        ConservativeRiskContract(501.0, 500.0, 500.0),
        ConservativeRiskContract(500.0, 500.0, 399.0),
    ],
)
def test_unknown_or_over_budget_risk_is_no_trade(
    contract: ConservativeRiskContract,
) -> None:
    result = assess_entry_risk(EXECUTABLE, scenarios(), contract)

    assert result.entry_eligible is False
    assert result.action is DecisionAction.NO_TRADE
    assert result.reason is NoTradeReason.RISK_BUDGET_EXCEEDED


def test_unexecutable_quote_is_no_trade_even_when_risk_fits() -> None:
    execution = ExecutableValue(NOW, False, ())
    result = assess_entry_risk(
        execution,
        scenarios(),
        ConservativeRiskContract(500.0, 500.0, 500.0),
    )
    assert result.entry_eligible is False
    assert result.reason is NoTradeReason.OPTION_CHAIN_UNUSABLE


def test_scenario_probabilities_and_values_are_strictly_validated() -> None:
    contract = ConservativeRiskContract(500.0, 500.0, 500.0)
    with pytest.raises(ValueError, match="sum to 1"):
        assess_entry_risk(
            EXECUTABLE,
            (PnlScenario("a", 0.6, 10.0), PnlScenario("b", 0.3, -10.0)),
            contract,
        )
    with pytest.raises(ValueError, match="finite"):
        PnlScenario("bad", 1.0, float("nan"))
    with pytest.raises(ValueError, match="finite"):
        PnlScenario("bad", float("inf"), 0.0)
    with pytest.raises(ValueError, match="exceeds"):
        assess_entry_risk(EXECUTABLE, scenarios(), ConservativeRiskContract(399.0, 500.0, 500.0))
