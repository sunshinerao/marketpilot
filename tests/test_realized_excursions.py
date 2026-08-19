from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from marketpilot.features.day_structure import MinuteBar
from marketpilot.validation.realized_excursions import (
    ExcursionCoverageError,
    realized_excursion,
)

BASE = datetime(2026, 8, 14, 14, 30, tzinfo=UTC)
ENTRY = BASE + timedelta(minutes=10)
CLOSE = BASE + timedelta(minutes=20)


def point(minutes: int, price: float) -> tuple[datetime, float]:
    return (BASE + timedelta(minutes=minutes), price)


def bar(minutes: int, open_: float, high: float, low: float, close: float) -> MinuteBar:
    return MinuteBar(
        ts=BASE + timedelta(minutes=minutes),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1_000.0,
    )


def test_monotonic_rise_extremes_and_timestamps() -> None:
    result = realized_excursion(
        (
            point(0, 100.0),
            point(10, 101.0),
            point(12, 103.0),
            point(14, 105.0),
            point(16, 107.0),
            point(18, 106.0),
            point(20, 104.0),
        ),
        entry=ENTRY,
        close=CLOSE,
    )

    assert result.entry_price == 101.0
    assert result.close_price == 104.0
    assert result.up_max == 6.0
    assert result.down_max == 0.0
    assert result.up_max_ts == BASE + timedelta(minutes=16)
    assert result.down_max_ts == BASE + timedelta(minutes=12)
    assert result.sample_count == 5


def test_monotonic_fall_extremes() -> None:
    result = realized_excursion(
        (
            point(10, 101.0),
            point(12, 99.0),
            point(14, 97.0),
            point(16, 97.5),
            point(18, 98.0),
            point(20, 98.0),
        ),
        entry=ENTRY,
        close=CLOSE,
    )

    assert result.up_max == 0.0
    assert result.down_max == 4.0
    assert result.up_max_ts == BASE + timedelta(minutes=12)
    assert result.down_max_ts == BASE + timedelta(minutes=14)


def test_asymmetric_excursions_on_minute_bars_use_highs_and_lows() -> None:
    result = realized_excursion(
        (
            bar(10, 100.0, 100.5, 99.5, 100.0),
            bar(12, 100.0, 108.0, 99.8, 107.5),
            bar(14, 107.5, 107.6, 107.0, 107.2),
            bar(16, 107.2, 107.2, 96.0, 96.5),
            bar(18, 96.5, 97.0, 96.2, 96.8),
            bar(20, 96.8, 97.0, 96.5, 96.9),
        ),
        entry=ENTRY,
        close=CLOSE,
    )

    assert result.entry_price == 100.0  # bar close at entry, not high/low
    assert result.up_max == 8.0
    assert result.down_max == 4.0
    assert result.up_max_ts == BASE + timedelta(minutes=12)
    assert result.down_max_ts == BASE + timedelta(minutes=16)
    assert result.close_price == 96.9
    assert result.sample_count == 5


def test_entry_price_lookback_uses_last_observation_at_or_before_entry() -> None:
    entry = BASE + timedelta(minutes=11)  # between the minute-10 and minute-12 prints
    result = realized_excursion(
        (
            point(9, 99.0),
            point(10, 100.0),
            point(12, 103.0),
            point(14, 102.5),
            point(16, 102.0),
            point(18, 101.5),
            point(20, 102.0),
        ),
        entry=entry,
        close=CLOSE,
    )

    assert result.entry_price == 100.0  # last print at or before entry, never forward-looking
    assert result.up_max == 3.0
    assert result.sample_count == 5


def test_gap_inside_window_rejected() -> None:
    with pytest.raises(ExcursionCoverageError):
        realized_excursion(
            (
                point(10, 100.0),
                point(12, 101.0),
                point(19, 102.0),  # 7-minute hole after minute 12
            ),
            entry=ENTRY,
            close=CLOSE,
        )


def test_gap_before_entry_rejected_as_stale_anchor() -> None:
    with pytest.raises(ExcursionCoverageError):
        realized_excursion(
            (
                point(0, 100.0),  # last print 10 minutes before entry
                point(12, 101.0),
                point(14, 101.5),
                point(20, 102.0),
            ),
            entry=ENTRY,
            close=CLOSE,
        )


def test_coverage_ending_before_close_rejected() -> None:
    with pytest.raises(ExcursionCoverageError):
        realized_excursion(
            (
                point(10, 100.0),
                point(12, 101.0),
                point(15, 102.0),  # nothing within max_gap_seconds of close
            ),
            entry=ENTRY,
            close=CLOSE,
        )


def test_custom_max_gap_seconds() -> None:
    series = (
        point(10, 100.0),
        point(14, 101.0),  # 4-minute gaps
        point(18, 101.5),
        point(20, 102.0),
    )
    with pytest.raises(ExcursionCoverageError):
        realized_excursion(series, entry=ENTRY, close=CLOSE, max_gap_seconds=180)
    result = realized_excursion(series, entry=ENTRY, close=CLOSE, max_gap_seconds=300)
    assert result.sample_count == 3


def test_observation_exactly_at_entry_is_anchor_not_extreme_sample() -> None:
    result = realized_excursion(
        (
            point(10, 130.0),  # huge price exactly at entry: anchor only, not an extreme
            point(12, 100.0),
            point(14, 100.5),
            point(16, 101.0),
            point(18, 101.0),
            point(20, 101.0),
        ),
        entry=ENTRY,
        close=CLOSE,
    )

    assert result.entry_price == 130.0
    assert result.up_max == 0.0
    assert result.down_max == 30.0
    assert result.sample_count == 5


def test_close_boundary_inclusion_and_post_close_exclusion() -> None:
    result = realized_excursion(
        (
            point(10, 100.0),
            point(12, 101.0),
            point(14, 102.0),
            point(16, 103.0),
            point(18, 104.0),
            point(20, 110.0),  # exactly at close: included
            point(21, 999.0),  # after close: excluded
        ),
        entry=ENTRY,
        close=CLOSE,
    )

    assert result.up_max == 10.0
    assert result.close_price == 110.0
    assert result.up_max_ts == CLOSE
    assert result.sample_count == 5


def test_naive_datetimes_rejected() -> None:
    series = (point(10, 100.0), point(12, 101.0), point(20, 102.0))
    with pytest.raises(ValueError, match="timezone-aware"):
        realized_excursion(series, entry=datetime(2026, 8, 14, 14, 40), close=CLOSE)
    with pytest.raises(ValueError, match="timezone-aware"):
        realized_excursion(series, entry=ENTRY, close=datetime(2026, 8, 14, 14, 50))
    naive_series = ((datetime(2026, 8, 14, 14, 40), 100.0), point(12, 101.0), point(20, 102.0))
    with pytest.raises(ValueError, match="timezone-aware"):
        realized_excursion(naive_series, entry=ENTRY, close=CLOSE)


def test_empty_series_rejected() -> None:
    with pytest.raises(ExcursionCoverageError):
        realized_excursion((), entry=ENTRY, close=CLOSE)


def test_entry_not_before_close_rejected() -> None:
    series = (point(10, 100.0), point(20, 101.0))
    with pytest.raises(ValueError, match="entry < close"):
        realized_excursion(series, entry=CLOSE, close=ENTRY)
    with pytest.raises(ValueError, match="entry < close"):
        realized_excursion(series, entry=ENTRY, close=ENTRY)


def test_no_observation_at_or_before_entry_rejected() -> None:
    with pytest.raises(ExcursionCoverageError):
        realized_excursion(
            (point(12, 101.0), point(20, 102.0)),
            entry=ENTRY,
            close=CLOSE,
        )


def test_unordered_series_rejected() -> None:
    with pytest.raises(ValueError, match="time ordered"):
        realized_excursion(
            (point(10, 100.0), point(15, 101.0), point(13, 102.0), point(20, 103.0)),
            entry=ENTRY,
            close=CLOSE,
        )


def test_non_finite_price_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        realized_excursion(
            (point(10, 100.0), point(12, float("nan")), point(20, 102.0)),
            entry=ENTRY,
            close=CLOSE,
        )


def test_mixed_series_rejected() -> None:
    mixed = (
        point(10, 100.0),
        bar(12, 100.0, 101.0, 99.0, 100.5),
        point(20, 102.0),
    )
    with pytest.raises(TypeError):
        realized_excursion(mixed, entry=ENTRY, close=CLOSE)  # type: ignore[arg-type]
