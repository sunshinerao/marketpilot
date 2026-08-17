from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from marketpilot.domain.market import MarketSnapshot


@dataclass(frozen=True, slots=True)
class ModelArtifactIdentity:
    """Exact identity of model code/configuration loaded by the decision runtime."""

    version: str
    artifact_hash: str


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    model_id: str
    name: str
    version: str
    artifact_hash: str
    asset_scope: tuple[str, ...]
    strategy_scope: tuple[str, ...]
    input_contract_version: str
    output_contract_version: str

    @property
    def artifact_identity(self) -> ModelArtifactIdentity:
        return ModelArtifactIdentity(
            version=self.version,
            artifact_hash=self.artifact_hash,
        )


class DecisionModel(Protocol):
    descriptor: ModelDescriptor

    def evaluate(self, snapshot: MarketSnapshot) -> Mapping[str, Any]: ...
