from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _finite(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    """A post-decision underlying and optional strategy mark observation."""

    observed_at: datetime
    underlying_price: float
    strategy_mtm_pnl: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        object.__setattr__(
            self,
            "underlying_price",
            _finite(self.underlying_price, "underlying_price"),
        )
        if self.underlying_price <= 0:
            raise ValueError("underlying_price must be positive")
        if self.strategy_mtm_pnl is not None:
            object.__setattr__(
                self,
                "strategy_mtm_pnl",
                _finite(self.strategy_mtm_pnl, "strategy_mtm_pnl"),
            )


@dataclass(frozen=True, slots=True)
class OutcomeLabelRequest:
    sample_id: str
    prediction_cutoff: datetime
    intraday_end: datetime
    expiry: datetime
    reference_price: float
    lower_level: float
    upper_level: float
    observations: tuple[OutcomeObservation, ...]

    def __post_init__(self) -> None:
        if not self.sample_id.strip():
            raise ValueError("sample_id must not be blank")
        cutoff = _utc(self.prediction_cutoff, "prediction_cutoff")
        intraday_end = _utc(self.intraday_end, "intraday_end")
        expiry = _utc(self.expiry, "expiry")
        if not cutoff < intraday_end <= expiry:
            raise ValueError("require prediction_cutoff < intraday_end <= expiry")
        reference = _finite(self.reference_price, "reference_price")
        lower = _finite(self.lower_level, "lower_level")
        upper = _finite(self.upper_level, "upper_level")
        if not 0 < lower < reference < upper:
            raise ValueError("require 0 < lower_level < reference_price < upper_level")
        if not self.observations:
            raise ValueError("at least one post-cutoff observation is required")
        times = tuple(observation.observed_at for observation in self.observations)
        if any(observed_at <= cutoff for observed_at in times):
            raise ValueError("outcome observations must be strictly after prediction_cutoff")
        if any(observed_at > expiry for observed_at in times):
            raise ValueError("outcome observations must not be after expiry")
        if any(left >= right for left, right in zip(times, times[1:], strict=False)):
            raise ValueError("outcome observations must be strictly time ordered")
        if times[-1] != expiry:
            raise ValueError("the final observation must be exactly at expiry")
        if not any(observed_at <= intraday_end for observed_at in times):
            raise ValueError("an intraday observation is required")
        object.__setattr__(self, "prediction_cutoff", cutoff)
        object.__setattr__(self, "intraday_end", intraday_end)
        object.__setattr__(self, "expiry", expiry)
        object.__setattr__(self, "reference_price", reference)
        object.__setattr__(self, "lower_level", lower)
        object.__setattr__(self, "upper_level", upper)


@dataclass(frozen=True, slots=True)
class OutcomeLabels:
    sample_id: str
    prediction_cutoff: datetime
    label_as_of: datetime
    maximum_upward_move: float
    maximum_downward_move: float
    upside_maximum_adverse_move: float
    downside_maximum_adverse_move: float
    intraday_upper_touch: bool
    intraday_lower_touch: bool
    expiry_upper_cross: bool
    expiry_lower_cross: bool
    maximum_mtm_loss: float | None


def generate_outcome_labels(request: OutcomeLabelRequest) -> OutcomeLabels:
    """Generate labels only from a complete, strictly post-cutoff outcome window."""

    prices = tuple(item.underlying_price for item in request.observations)
    intraday = tuple(
        item for item in request.observations if item.observed_at <= request.intraday_end
    )
    expiry_price = request.observations[-1].underlying_price
    mtm_values = tuple(
        item.strategy_mtm_pnl for item in request.observations if item.strategy_mtm_pnl is not None
    )
    return OutcomeLabels(
        sample_id=request.sample_id,
        prediction_cutoff=request.prediction_cutoff,
        label_as_of=request.expiry,
        maximum_upward_move=max(0.0, max(prices) - request.reference_price),
        maximum_downward_move=max(0.0, request.reference_price - min(prices)),
        # A bullish view is hurt by a fall; a bearish view is hurt by a rise.
        upside_maximum_adverse_move=max(0.0, request.reference_price - min(prices)),
        downside_maximum_adverse_move=max(0.0, max(prices) - request.reference_price),
        intraday_upper_touch=any(item.underlying_price >= request.upper_level for item in intraday),
        intraday_lower_touch=any(item.underlying_price <= request.lower_level for item in intraday),
        expiry_upper_cross=expiry_price >= request.upper_level,
        expiry_lower_cross=expiry_price <= request.lower_level,
        maximum_mtm_loss=(max(0.0, -min(mtm_values)) if mtm_values else None),
    )
