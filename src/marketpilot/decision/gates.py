from __future__ import annotations

from dataclasses import dataclass

from marketpilot.domain.decision import NoTradeReason
from marketpilot.domain.market import DataQuality


@dataclass(frozen=True, slots=True)
class DecisionGateContext:
    data_quality: DataQuality
    contract_matches: bool = True
    event_cleared: bool = True
    unscheduled_shock: bool = False
    option_chain_usable: bool = True
    tail_expanding: bool = False
    next_major_event_in_holding_period: bool = False
    edge_ok: bool = True
    risk_budget_ok: bool = True


class RiskGate:
    def evaluate(self, context: DecisionGateContext) -> tuple[NoTradeReason, ...]:
        reasons: list[NoTradeReason] = []
        if context.data_quality is not DataQuality.GREEN:
            reasons.append(NoTradeReason.STALE_MARKET_DATA)
        if not context.contract_matches:
            reasons.append(NoTradeReason.CONTRACT_MISMATCH)
        if not context.event_cleared:
            reasons.append(NoTradeReason.EVENT_PENDING)
        if context.unscheduled_shock:
            reasons.append(NoTradeReason.UNSCHEDULED_SHOCK)
        if not context.option_chain_usable:
            reasons.append(NoTradeReason.OPTION_CHAIN_UNUSABLE)
        if context.tail_expanding:
            reasons.append(NoTradeReason.TAIL_EXPANDING)
        if context.next_major_event_in_holding_period:
            reasons.append(NoTradeReason.NEXT_EVENT_TOO_CLOSE)
        if not context.edge_ok:
            reasons.append(NoTradeReason.NEGATIVE_EDGE)
        if not context.risk_budget_ok:
            reasons.append(NoTradeReason.RISK_BUDGET_EXCEEDED)
        return tuple(reasons)
