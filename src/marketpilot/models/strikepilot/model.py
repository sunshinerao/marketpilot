from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from marketpilot.domain.market import MarketSnapshot
from marketpilot.models.base import ModelDescriptor
from marketpilot.models.strikepilot.strikes import select_iron_condor_strikes


class StrikePilotModel:
    descriptor = ModelDescriptor(
        model_id="strikepilot_spxw_0dte_ic",
        name="StrikePilot",
        version="0.1.0-baseline",
        asset_scope=("INDEX", "FUTURE", "OPTION"),
        strategy_scope=("SPXW_0DTE_IRON_CONDOR",),
        input_contract_version="strikepilot-input-v1",
        output_contract_version="strike-map-v1",
    )

    def evaluate(self, snapshot: MarketSnapshot) -> Mapping[str, Any]:
        required = ("center", "up_tail", "down_tail")
        missing = [key for key in required if key not in snapshot.values]
        if missing:
            raise ValueError(f"missing StrikePilot inputs: {', '.join(missing)}")
        strikes = select_iron_condor_strikes(
            center=float(snapshot.values["center"]),
            up_tail=float(snapshot.values["up_tail"]),
            down_tail=float(snapshot.values["down_tail"]),
            joint_buffer=float(snapshot.values.get("joint_buffer", 0.0)),
        )
        return {
            "decision_type": "STRIKE_MAP",
            "short_put": strikes.short_put,
            "long_put": strikes.long_put,
            "short_call": strikes.short_call,
            "long_call": strikes.long_call,
            "put_distance": strikes.put_distance,
            "call_distance": strikes.call_distance,
        }
