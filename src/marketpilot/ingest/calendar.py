from __future__ import annotations

import tomllib
from datetime import date, time, timedelta
from pathlib import Path

from marketpilot.domain.trading_calendar import (
    EquityCalendarConfig,
    TradingDayStatus,
    USEquityCalendar,
)


def load_equity_calendar(path: str | Path) -> USEquityCalendar:
    """Load a version-bounded equity calendar; out-of-range days stay UNVERIFIED."""

    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    early_closes = {
        date.fromisoformat(entry["session_date"]): time.fromisoformat(entry["closes_at"])
        for entry in raw.get("early_closes", [])
    }
    return USEquityCalendar(
        EquityCalendarConfig(
            verified_from=date.fromisoformat(raw["verified_from"]),
            verified_through=date.fromisoformat(raw["verified_through"]),
            holidays=frozenset(
                date.fromisoformat(value) for value in raw.get("holidays", [])
            ),
            early_closes=early_closes,
        )
    )


def trading_days(calendar: USEquityCalendar, start: date, end: date) -> tuple[date, ...]:
    """Expand the inclusive window to days the versioned calendar proves tradable."""

    if start > end:
        raise ValueError("start must not be after end")
    days: list[date] = []
    current = start
    while current <= end:
        if calendar.day_status(current) is TradingDayStatus.TRADING_DAY:
            days.append(current)
        current += timedelta(days=1)
    return tuple(days)
