from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class FreshnessThresholds:
    futures: int
    index: int
    volatility_index: int
    option_chain: int


@dataclass(frozen=True, slots=True)
class RulesConfig:
    version: str
    timezone: str
    strike_increment: int
    wing_width: int
    freshness_seconds: FreshnessThresholds


def load_rules(path: str | Path) -> RulesConfig:
    with Path(path).open("rb") as stream:
        raw: Mapping[str, Any] = tomllib.load(stream)
    freshness = raw["freshness_seconds"]
    return RulesConfig(
        version=str(raw["version"]),
        timezone=str(raw["timezone"]),
        strike_increment=int(raw["strike_increment"]),
        wing_width=int(raw["wing_width"]),
        freshness_seconds=FreshnessThresholds(
            futures=int(freshness["futures"]),
            index=int(freshness["index"]),
            volatility_index=int(freshness["volatility_index"]),
            option_chain=int(freshness["option_chain"]),
        ),
    )
