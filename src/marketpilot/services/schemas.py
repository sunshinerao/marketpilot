from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from marketpilot.domain.decision import DecisionAction, NoTradeReason
from marketpilot.domain.market import DataQuality


class GateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_quality: DataQuality = DataQuality.RED
    contract_matches: bool = True
    event_cleared: bool = False
    unscheduled_shock: bool = False
    option_chain_usable: bool = False
    tail_expanding: bool = False
    next_major_event_in_holding_period: bool = False
    edge_ok: bool = False
    risk_budget_ok: bool = True


class DecisionRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = "strikepilot_spxw_0dte_ic"
    as_of: datetime
    values: dict[str, Any] = Field(default_factory=dict)
    gates: GateInput = Field(default_factory=GateInput)


class DecisionRunOutput(BaseModel):
    run_id: str
    platform: str = "MarketPilot"
    model_id: str
    model_version: str
    rules_version: str
    snapshot_id: str
    data_as_of: datetime
    action: DecisionAction
    reasons: list[NoTradeReason]
    output: dict[str, Any]
