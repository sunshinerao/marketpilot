from datetime import UTC, date, datetime, time

import pytest

from marketpilot.domain.trading_calendar import (
    EquityCalendarConfig,
    GlobexCalendarConfig,
    GlobexSessionClock,
    SessionState,
    TradingDayStatus,
    USEquityCalendar,
)


def equity_calendar() -> USEquityCalendar:
    return USEquityCalendar(
        EquityCalendarConfig(
            verified_from=date(2026, 1, 1),
            verified_through=date(2026, 12, 31),
            holidays=frozenset({date(2026, 7, 3)}),
            early_closes={date(2026, 11, 27): time(13, 0)},
        )
    )


def test_regular_session_uses_new_york_dst_and_fixed_cutoff_anchor() -> None:
    calendar = equity_calendar()

    winter = calendar.session(date(2026, 1, 5))
    summer = calendar.session(date(2026, 7, 6))

    assert winter.status is TradingDayStatus.TRADING_DAY
    assert winter.preopen_cutoff_at == datetime.fromisoformat("2026-01-05T09:29:59-05:00")
    assert winter.anchor_at == datetime.fromisoformat("2026-01-05T16:00:00-05:00")
    assert summer.preopen_cutoff_at == datetime.fromisoformat("2026-07-06T09:29:59-04:00")
    assert summer.anchor_at == datetime.fromisoformat("2026-07-06T16:00:00-04:00")


def test_weekend_holiday_half_day_and_unverified_dates_fail_closed() -> None:
    calendar = equity_calendar()

    assert calendar.session(date(2026, 7, 4)).status is TradingDayStatus.WEEKEND
    assert calendar.session(date(2026, 7, 3)).status is TradingDayStatus.HOLIDAY
    assert calendar.session(date(2027, 1, 4)).status is TradingDayStatus.UNVERIFIED
    assert calendar.session(date(2027, 1, 4)).permits_trading is False

    half_day = calendar.session(date(2026, 11, 27))
    assert half_day.closes_at == datetime.fromisoformat("2026-11-27T13:00:00-05:00")
    # Never claim a nonexistent 16:00 official cash anchor on a half-day.
    assert half_day.anchor_at == datetime.fromisoformat("2026-11-27T13:00:00-05:00")
    assert half_day.is_early_close is True


def test_invalid_calendar_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        EquityCalendarConfig(
            verified_from=date(2026, 1, 1),
            verified_through=date(2026, 12, 31),
            timezone="UTC",
        )
    with pytest.raises(ValueError, match="both a holiday"):
        EquityCalendarConfig(
            verified_from=date(2026, 1, 1),
            verified_through=date(2026, 12, 31),
            holidays=frozenset({date(2026, 11, 27)}),
            early_closes={date(2026, 11, 27): time(13)},
        )


def test_globex_maintenance_weekend_and_unverified_ranges() -> None:
    clock = GlobexSessionClock(
        GlobexCalendarConfig(
            verified_from=date(2026, 8, 1),
            verified_through=date(2026, 8, 31),
            closed_dates=frozenset({date(2026, 8, 19)}),
        )
    )

    # UTC inputs prove the clock converts to America/New_York during DST.
    assert clock.state_at(datetime(2026, 8, 18, 20, 59, tzinfo=UTC)) is SessionState.OPEN
    assert clock.state_at(datetime(2026, 8, 18, 21, 0, tzinfo=UTC)) is SessionState.MAINTENANCE
    assert clock.state_at(datetime(2026, 8, 18, 21, 59, 59, tzinfo=UTC)) is SessionState.MAINTENANCE
    assert clock.state_at(datetime(2026, 8, 18, 22, 0, tzinfo=UTC)) is SessionState.OPEN
    assert clock.state_at(datetime(2026, 8, 22, 16, 0, tzinfo=UTC)) is SessionState.CLOSED
    assert clock.state_at(datetime(2026, 8, 23, 21, 59, tzinfo=UTC)) is SessionState.CLOSED
    assert clock.state_at(datetime(2026, 8, 23, 22, 0, tzinfo=UTC)) is SessionState.OPEN
    assert clock.state_at(datetime(2026, 8, 19, 16, 0, tzinfo=UTC)) is SessionState.CLOSED
    assert clock.state_at(datetime(2026, 9, 1, 16, 0, tzinfo=UTC)) is SessionState.UNVERIFIED


def test_globex_rejects_naive_instants() -> None:
    clock = GlobexSessionClock(GlobexCalendarConfig(date(2026, 1, 1), date(2026, 12, 31)))
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.state_at(datetime(2026, 8, 18, 17, 30))
