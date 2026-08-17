from datetime import UTC, datetime

from marketpilot.decision.gates import DecisionGateContext
from marketpilot.decision.runner import DecisionRunner
from marketpilot.domain.decision import DecisionAction, NoTradeReason
from marketpilot.domain.market import DataQuality, DataQualityReport, MarketSnapshot
from marketpilot.models.base import ModelArtifactIdentity
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


def test_governed_version_mismatch_fails_closed_without_model_output() -> None:
    registry = ModelRegistry()
    registry.register(StrikePilotModel())
    snapshot = MarketSnapshot(
        snapshot_id="sha256:governance-mismatch",
        as_of=datetime(2026, 8, 17, 13, 45, tzinfo=UTC),
        quality=DataQualityReport(status=DataQuality.GREEN),
        values={"center": 7800, "up_tail": 20, "down_tail": 20},
    )

    result = DecisionRunner(registry).run(
        model_id="strikepilot_spxw_0dte_ic",
        snapshot=snapshot,
        gates=DecisionGateContext(data_quality=DataQuality.GREEN),
        required_model_identity=ModelArtifactIdentity(
            version="9.9.9-promoted-but-not-loaded",
            artifact_hash="sha256:not-loaded",
        ),
    )

    assert result.action is DecisionAction.NO_TRADE
    assert result.reasons == (NoTradeReason.MODEL_VERSION_NOT_LOADED,)
    assert result.output == {}


def test_governed_artifact_hash_mismatch_fails_closed_even_when_version_matches() -> None:
    registry = ModelRegistry()
    model = StrikePilotModel()
    registry.register(model)
    snapshot = MarketSnapshot(
        snapshot_id="sha256:governance-artifact-mismatch",
        as_of=datetime(2026, 8, 17, 13, 45, tzinfo=UTC),
        quality=DataQualityReport(status=DataQuality.GREEN),
        values={"center": 7800, "up_tail": 20, "down_tail": 20},
    )

    result = DecisionRunner(registry).run(
        model_id=model.descriptor.model_id,
        snapshot=snapshot,
        gates=DecisionGateContext(data_quality=DataQuality.GREEN),
        required_model_identity=ModelArtifactIdentity(
            version=model.descriptor.version,
            artifact_hash="sha256:different-artifact",
        ),
    )

    assert result.action is DecisionAction.NO_TRADE
    assert result.reasons == (NoTradeReason.MODEL_VERSION_NOT_LOADED,)
    assert result.output == {}


def test_strikepilot_artifact_identity_is_deterministic_and_config_bound() -> None:
    first = StrikePilotModel(strike_increment=5, wing_width=5)
    same = StrikePilotModel(strike_increment=5, wing_width=5)
    changed = StrikePilotModel(strike_increment=5, wing_width=10)

    assert first.descriptor.artifact_identity == same.descriptor.artifact_identity
    assert first.descriptor.version == changed.descriptor.version
    assert first.descriptor.artifact_hash != changed.descriptor.artifact_hash
