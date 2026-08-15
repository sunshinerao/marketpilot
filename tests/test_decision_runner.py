from datetime import UTC, datetime

from marketpilot.decision.gates import DecisionGateContext
from marketpilot.decision.runner import DecisionRunner
from marketpilot.domain.decision import DecisionAction, NoTradeReason
from marketpilot.domain.market import DataQuality, DataQualityReport, MarketSnapshot
from marketpilot.models.registry import ModelRegistry
from marketpilot.models.strikepilot.model import StrikePilotModel


def test_no_trade_run_is_auditable_and_does_not_fabricate_legs() -> None:
    registry = ModelRegistry()
    registry.register(StrikePilotModel())
    snapshot = MarketSnapshot(
        snapshot_id="sha256:test",
        as_of=datetime(2026, 8, 17, 13, 45, tzinfo=UTC),
        quality=DataQualityReport(status=DataQuality.RED),
        values={},
    )
    result = DecisionRunner(registry).run(
        model_id="strikepilot_spxw_0dte_ic",
        snapshot=snapshot,
        gates=DecisionGateContext(data_quality=DataQuality.RED),
    )

    assert result.action is DecisionAction.NO_TRADE
    assert result.reasons == (NoTradeReason.STALE_MARKET_DATA,)
    assert result.snapshot_id == "sha256:test"
    assert result.output == {}
