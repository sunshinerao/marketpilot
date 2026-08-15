from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class DataQuality(StrEnum):
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    symbol: str
    venue: str
    asset_class: str
    timezone: str
    expiry: str | None = None
    multiplier: float | None = None
    tick_size: float | None = None


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    status: DataQuality
    stale_fields: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    snapshot_id: str
    as_of: datetime
    quality: DataQualityReport
    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
