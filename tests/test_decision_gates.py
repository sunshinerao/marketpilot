from marketpilot.decision.gates import DecisionGateContext, RiskGate
from marketpilot.domain.decision import NoTradeReason
from marketpilot.domain.market import DataQuality


def test_risk_gate_returns_stable_explainable_reasons() -> None:
    reasons = RiskGate().evaluate(
        DecisionGateContext(
            data_quality=DataQuality.RED,
            event_cleared=False,
            option_chain_usable=False,
        )
    )
    assert reasons == (
        NoTradeReason.STALE_MARKET_DATA,
        NoTradeReason.EVENT_PENDING,
        NoTradeReason.OPTION_CHAIN_UNUSABLE,
    )
