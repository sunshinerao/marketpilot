from __future__ import annotations

from uuid import uuid4

from marketpilot.decision.gates import DecisionGateContext, RiskGate
from marketpilot.domain.decision import DecisionAction, DecisionResult
from marketpilot.domain.market import MarketSnapshot
from marketpilot.models.registry import ModelRegistry


class DecisionRunner:
    def __init__(self, registry: ModelRegistry, rules_version: str = "rules-v1") -> None:
        self._registry = registry
        self._rules_version = rules_version
        self._gate = RiskGate()

    def run(
        self,
        *,
        model_id: str,
        snapshot: MarketSnapshot,
        gates: DecisionGateContext,
    ) -> DecisionResult:
        model = self._registry.get(model_id)
        reasons = self._gate.evaluate(gates)
        if reasons:
            action = DecisionAction.NO_TRADE
            output: dict[str, object] = {}
        else:
            action = DecisionAction.WAIT
            output = dict(model.evaluate(snapshot))

        return DecisionResult(
            run_id=str(uuid4()),
            model_id=model.descriptor.model_id,
            model_version=model.descriptor.version,
            rules_version=self._rules_version,
            snapshot_id=snapshot.snapshot_id,
            data_as_of=snapshot.as_of,
            action=action,
            reasons=reasons,
            output=output,
        )
