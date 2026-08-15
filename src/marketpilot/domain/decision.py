from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class DecisionAction(StrEnum):
    ENTER = "ENTER"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"


class NoTradeReason(StrEnum):
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
    EVENT_PENDING = "EVENT_PENDING"
    UNSCHEDULED_SHOCK = "UNSCHEDULED_SHOCK"
    OPTION_CHAIN_UNUSABLE = "OPTION_CHAIN_UNUSABLE"
    TAIL_EXPANDING = "TAIL_EXPANDING"
    NEXT_EVENT_TOO_CLOSE = "NEXT_EVENT_TOO_CLOSE"
    NEGATIVE_EDGE = "NEGATIVE_EDGE"
    RISK_BUDGET_EXCEEDED = "RISK_BUDGET_EXCEEDED"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    run_id: str
    model_id: str
    model_version: str
    rules_version: str
    snapshot_id: str
    data_as_of: datetime
    action: DecisionAction
    reasons: tuple[NoTradeReason, ...] = ()
    output: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "output", MappingProxyType(dict(self.output)))
