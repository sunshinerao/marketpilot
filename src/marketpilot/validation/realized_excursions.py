"""Workstream C: realized up/down max excursions over an exact entry→close window.

Strict label contract (mirrors ``validation/outcome_labels.py``):

- entry/close must be timezone-aware and entry < close;
- the entry price is the last observation at or before ``entry`` (never a
  forward-looking print);
- extreme moves use only observations strictly after ``entry`` and at or
  before ``close``;
- the series must cover [entry, close] with no gap longer than
  ``max_gap_seconds`` between consecutive observations — insufficient
  coverage raises :class:`ExcursionCoverageError` and never yields a label.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from marketpilot.features.day_structure import MinuteBar


class ExcursionCoverageError(ValueError):
    """Raised when a series cannot support an honest excursion label."""


type PricePoint = tuple[datetime, float]
type ExcursionSeries = tuple[PricePoint, ...] | tuple[MinuteBar, ...]

# Normalized observation: (ts, high, low, reference price).
type _Observation = tuple[datetime, float, float, float]


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
class ExcursionResult:
    """Realized extreme excursions from entry to close, in price units."""

    entry_ts: datetime
    close_ts: datetime
    entry_price: float
    close_price: float
    up_max: float
    down_max: float
    up_max_ts: datetime
    down_max_ts: datetime
    sample_count: int


def _normalize(series: ExcursionSeries) -> tuple[_Observation, ...]:
    if not series:
        raise ExcursionCoverageError("series must not be empty")
    first = series[0]
    observations: list[_Observation] = []
    if isinstance(first, MinuteBar):
        for item in series:
            if not isinstance(item, MinuteBar):
                raise TypeError("series must not mix MinuteBar and (ts, price) items")
            observations.append((item.ts, item.high, item.low, item.close))
    elif isinstance(first, tuple):
        for item in series:
            if isinstance(item, MinuteBar) or not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("series items must all be MinuteBar or all be (ts, price) pairs")
            ts, price = item
            if not isinstance(ts, datetime):
                raise TypeError("series timestamps must be datetime instances")
            reference = _finite(price, "series price")
            observations.append((_utc(ts, "series timestamp"), reference, reference, reference))
    else:
        raise TypeError("series items must all be MinuteBar or all be (ts, price) pairs")
    normalized = tuple(observations)
    times = tuple(observation[0] for observation in normalized)
    if any(left >= right for left, right in zip(times, times[1:], strict=False)):
        raise ValueError("series observations must be strictly time ordered")
    return normalized


def realized_excursion(
    series: ExcursionSeries,
    *,
    entry: datetime,
    close: datetime,
    max_gap_seconds: int = 180,
) -> ExcursionResult:
    """Compute realized up/down max excursions over the exact (entry, close] window.

    The entry price is the reference price of the last observation at or
    before ``entry``. Extremes use highs/lows of observations strictly after
    ``entry`` and at or before ``close``. Any coverage hole — no observation
    at or before ``entry``, an inter-observation gap above
    ``max_gap_seconds`` inside the window, or coverage ending before
    ``close`` — raises :class:`ExcursionCoverageError`.
    """

    entry_ts = _utc(entry, "entry")
    close_ts = _utc(close, "close")
    if not entry_ts < close_ts:
        raise ValueError("require entry < close")
    if max_gap_seconds <= 0:
        raise ValueError("max_gap_seconds must be positive")
    observations = _normalize(series)

    anchor_index: int | None = None
    for index, observation in enumerate(observations):
        if observation[0] <= entry_ts:
            anchor_index = index
        else:
            break
    if anchor_index is None:
        raise ExcursionCoverageError("no observation at or before entry")

    window = tuple(
        observation for observation in observations if entry_ts < observation[0] <= close_ts
    )
    if not window:
        raise ExcursionCoverageError("no observations strictly after entry and at or before close")

    previous_ts = observations[anchor_index][0]
    for observation in window:
        gap = (observation[0] - previous_ts).total_seconds()
        if gap > max_gap_seconds:
            raise ExcursionCoverageError(
                f"coverage gap of {gap:.0f}s exceeds max_gap_seconds={max_gap_seconds}"
            )
        previous_ts = observation[0]
    tail_gap = (close_ts - previous_ts).total_seconds()
    if tail_gap > max_gap_seconds:
        raise ExcursionCoverageError(
            f"coverage ends {tail_gap:.0f}s before close, exceeding "
            f"max_gap_seconds={max_gap_seconds}"
        )

    entry_price = observations[anchor_index][3]
    up_high, up_ts = window[0][1], window[0][0]
    down_low, down_ts = window[0][2], window[0][0]
    for observation in window[1:]:
        if observation[1] > up_high:
            up_high, up_ts = observation[1], observation[0]
        if observation[2] < down_low:
            down_low, down_ts = observation[2], observation[0]

    return ExcursionResult(
        entry_ts=entry_ts,
        close_ts=close_ts,
        entry_price=entry_price,
        close_price=window[-1][3],
        up_max=max(0.0, up_high - entry_price),
        down_max=max(0.0, entry_price - down_low),
        up_max_ts=up_ts,
        down_max_ts=down_ts,
        sample_count=len(window),
    )
