from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime


class DayStructureError(ValueError):
    """Raised when a normalized day violates structural invariants."""


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DayStructureError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class MinuteBar:
    """One underlying (ES) minute bar in UTC."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", _utc(self.ts, "ts"))
        if self.low > self.high:
            raise DayStructureError("low must not exceed high")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise DayStructureError("open/close must lie within [low, high]")
        if self.volume < 0:
            raise DayStructureError("volume must not be negative")


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """One 0DTE contract's minute NBBO in UTC."""

    ts: datetime
    symbol: str
    bid: float | None
    ask: float | None
    bid_size: int
    ask_size: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", _utc(self.ts, "ts"))
        if len(self.symbol) != 21:
            raise DayStructureError("symbol must be the 21-character padded OSI form")
        if self.bid is not None and self.bid < 0:
            raise DayStructureError("bid must not be negative")
        if self.ask is not None and self.ask < 0:
            raise DayStructureError("ask must not be negative")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise DayStructureError("bid must not exceed ask")
        if self.bid_size < 0 or self.ask_size < 0:
            raise DayStructureError("sizes must not be negative")


@dataclass(frozen=True, slots=True)
class ChainDay:
    """One trading day: underlying minute bars plus the 0DTE chain NBBO."""

    day: date
    underlying_bars: tuple[MinuteBar, ...]
    quotes: tuple[OptionQuote, ...]

    def __post_init__(self) -> None:
        bars = self.underlying_bars
        quotes = self.quotes
        if not bars:
            raise DayStructureError("underlying_bars must not be empty")
        if not quotes:
            raise DayStructureError("quotes must not be empty")
        if any(bars[i].ts > bars[i + 1].ts for i in range(len(bars) - 1)):
            raise DayStructureError("underlying_bars must be time-ordered")
        if any(quotes[i].ts > quotes[i + 1].ts for i in range(len(quotes) - 1)):
            raise DayStructureError("quotes must be time-ordered")
