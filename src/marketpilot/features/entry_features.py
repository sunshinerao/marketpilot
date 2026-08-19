from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime


class EntryFeaturesError(ValueError):
    """Raised when an entry-time feature record violates its contract."""


@dataclass(frozen=True, slots=True)
class EntryFeatures:
    """Feature snapshot at the candidate entry instant (default 09:45 ET).

    All volatility quantities are decimals (0.12 = 12%). `atm_iv_valid=False`
    means the inversion failed and `atm_iv`/`skew` must not be consumed; the
    record is still emitted so the day stays auditable.
    """

    day: date
    entry_ts: datetime
    implied_center: float
    atm_iv: float
    skew: float
    realized_vol_30m: float
    median_spread: float
    atm_iv_valid: bool

    def __post_init__(self) -> None:
        if self.entry_ts.tzinfo is None or self.entry_ts.utcoffset() is None:
            raise EntryFeaturesError("entry_ts must be timezone-aware")
        object.__setattr__(self, "entry_ts", self.entry_ts.astimezone(UTC))
        if self.implied_center <= 0:
            raise EntryFeaturesError("implied_center must be positive")
        if self.atm_iv_valid and not 0 < self.atm_iv < 5:
            raise EntryFeaturesError("atm_iv must be within (0, 5)")
        if self.realized_vol_30m < 0:
            raise EntryFeaturesError("realized_vol_30m must not be negative")
        if self.median_spread < 0:
            raise EntryFeaturesError("median_spread must not be negative")
