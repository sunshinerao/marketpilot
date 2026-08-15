from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from marketpilot.domain.market import MarketSnapshot


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    model_id: str
    name: str
    version: str
    asset_scope: tuple[str, ...]
    strategy_scope: tuple[str, ...]
    input_contract_version: str
    output_contract_version: str


class DecisionModel(Protocol):
    descriptor: ModelDescriptor

    def evaluate(self, snapshot: MarketSnapshot) -> Mapping[str, Any]: ...
