from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class FreshnessThresholds:
    futures: int
    index: int
    volatility_index: int
    option_chain: int

    def __post_init__(self) -> None:
        if min(self.futures, self.index, self.volatility_index, self.option_chain) <= 0:
            raise ValueError("freshness thresholds must be positive")


@dataclass(frozen=True, slots=True)
class RiskThresholds:
    normal_quantile: float
    p1_quantile: float
    p0_quantile: float
    single_side_expiry_cross_max: float
    single_side_touch_max: float
    initial_iv_rv_ratio: float

    def __post_init__(self) -> None:
        if not 0 < self.normal_quantile <= self.p1_quantile <= self.p0_quantile < 1:
            raise ValueError("risk quantiles must be ordered inside (0, 1)")
        if not 0 <= self.single_side_expiry_cross_max <= 1:
            raise ValueError("single_side_expiry_cross_max must be in [0, 1]")
        if not 0 <= self.single_side_touch_max <= 1:
            raise ValueError("single_side_touch_max must be in [0, 1]")
        if self.initial_iv_rv_ratio <= 0:
            raise ValueError("initial_iv_rv_ratio must be positive")


@dataclass(frozen=True, slots=True)
class SessionRules:
    anchor_et: str
    preopen_cutoff_et: str
    primary_brief_et: str
    globex_maintenance_start_et: str
    globex_maintenance_end_et: str

    def __post_init__(self) -> None:
        parsed = {
            name: time.fromisoformat(value)
            for name, value in (
                ("anchor_et", self.anchor_et),
                ("preopen_cutoff_et", self.preopen_cutoff_et),
                ("primary_brief_et", self.primary_brief_et),
                ("globex_maintenance_start_et", self.globex_maintenance_start_et),
                ("globex_maintenance_end_et", self.globex_maintenance_end_et),
            )
        }
        if parsed["preopen_cutoff_et"] >= parsed["primary_brief_et"]:
            raise ValueError("preopen cutoff must precede the primary brief")
        if parsed["globex_maintenance_start_et"] >= parsed["globex_maintenance_end_et"]:
            raise ValueError("Globex maintenance start must precede its end")


@dataclass(frozen=True, slots=True)
class RulesConfig:
    version: str
    timezone: str
    strike_increment: int
    wing_width: int
    freshness_seconds: FreshnessThresholds
    risk: RiskThresholds
    sessions: SessionRules

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("rules version must not be blank")
        if self.timezone != "America/New_York":
            raise ValueError("rules timezone must be America/New_York")
        if self.strike_increment <= 0 or self.wing_width <= 0:
            raise ValueError("strike increment and wing width must be positive")
        if self.wing_width % self.strike_increment != 0:
            raise ValueError("wing width must align to the strike increment")


def load_rules(path: str | Path) -> RulesConfig:
    with Path(path).open("rb") as stream:
        raw: Mapping[str, Any] = tomllib.load(stream)
    freshness = raw["freshness_seconds"]
    risk = raw["risk"]
    sessions = raw["sessions"]
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
        risk=RiskThresholds(
            normal_quantile=float(risk["normal_quantile"]),
            p1_quantile=float(risk["p1_quantile"]),
            p0_quantile=float(risk["p0_quantile"]),
            single_side_expiry_cross_max=float(risk["single_side_expiry_cross_max"]),
            single_side_touch_max=float(risk["single_side_touch_max"]),
            initial_iv_rv_ratio=float(risk["initial_iv_rv_ratio"]),
        ),
        sessions=SessionRules(
            anchor_et=str(sessions["anchor_et"]),
            preopen_cutoff_et=str(sessions["preopen_cutoff_et"]),
            primary_brief_et=str(sessions["primary_brief_et"]),
            globex_maintenance_start_et=str(sessions["globex_maintenance_start_et"]),
            globex_maintenance_end_et=str(sessions["globex_maintenance_end_et"]),
        ),
    )
