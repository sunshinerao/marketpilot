from __future__ import annotations

from dataclasses import dataclass
from datetime import date


class TailDistancesError(ValueError):
    """Raised when model-recommended tail distances violate their contract."""


@dataclass(frozen=True, slots=True)
class TailDistances:
    """Model-recommended short-leg distances for one trading day.

    Distances are points of SPX from the implied center to the short put/call
    strikes, respectively for the downside (down_distance) and upside
    (up_distance) tails.
    """

    day: date
    down_distance: float
    up_distance: float
    regime: str
    model_version: str
    quantile: float

    def __post_init__(self) -> None:
        if self.down_distance <= 0 or self.up_distance <= 0:
            raise TailDistancesError("distances must be positive")
        if not self.regime.strip():
            raise TailDistancesError("regime must not be blank")
        if not self.model_version.strip():
            raise TailDistancesError("model_version must not be blank")
        if not 0 < self.quantile < 1:
            raise TailDistancesError("quantile must be within (0, 1)")
