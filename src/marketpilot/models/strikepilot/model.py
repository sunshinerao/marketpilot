from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from marketpilot.domain.market import MarketSnapshot
from marketpilot.domain.snapshot import freeze_snapshot
from marketpilot.models.base import ModelDescriptor
from marketpilot.models.strikepilot.strikes import select_iron_condor_strikes

MODEL_ID = "strikepilot_spxw_0dte_ic"
MODEL_VERSION = "0.1.0-baseline"


def strikepilot_artifact_hash(*, strike_increment: int, wing_width: int) -> str:
    """Return the declared runtime contract identity used by governance.

    The default identity is intentionally stable for the append-only 0.1.0 baseline
    already present in durable stores. Any parameter change receives a distinct hash
    and must be represented by a newly governed model version before it can run.
    """

    if (strike_increment, wing_width) == (5, 5):
        return freeze_snapshot(
            {"model_id": MODEL_ID, "version": MODEL_VERSION, "kind": "baseline"}
        ).snapshot_id

    return freeze_snapshot(
        {
            "artifact_contract": "marketpilot-decision-model-v1",
            "implementation": "marketpilot.models.strikepilot.model.StrikePilotModel",
            "implementation_revision": "strikepilot-spxw-0dte-ic-v1",
            "model_id": MODEL_ID,
            "version": MODEL_VERSION,
            "parameters": {
                "strike_increment": strike_increment,
                "wing_width": wing_width,
            },
            "input_contract_version": "strikepilot-input-v1",
            "output_contract_version": "strike-map-v1",
        }
    ).snapshot_id


class StrikePilotModel:
    def __init__(self, *, strike_increment: int = 5, wing_width: int = 5) -> None:
        if strike_increment <= 0 or wing_width <= 0:
            raise ValueError("strike_increment and wing_width must be positive")
        self.descriptor = ModelDescriptor(
            model_id=MODEL_ID,
            name="StrikePilot",
            version=MODEL_VERSION,
            artifact_hash=strikepilot_artifact_hash(
                strike_increment=strike_increment,
                wing_width=wing_width,
            ),
            asset_scope=("INDEX", "FUTURE", "OPTION"),
            strategy_scope=("SPXW_0DTE_IRON_CONDOR",),
            input_contract_version="strikepilot-input-v1",
            output_contract_version="strike-map-v1",
        )
        self._strike_increment = strike_increment
        self._wing_width = wing_width

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
            strike_increment=self._strike_increment,
            wing_width=self._wing_width,
        )
        return {
            "decision_type": "STRIKE_MAP",
            "reference_center": float(snapshot.values["center"]),
            "short_put": strikes.short_put,
            "long_put": strikes.long_put,
            "short_call": strikes.short_call,
            "long_call": strikes.long_call,
            "put_distance": strikes.put_distance,
            "call_distance": strikes.call_distance,
        }
