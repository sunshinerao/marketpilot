from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from types import MappingProxyType
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


class TradingDayStatus(StrEnum):
    TRADING_DAY = "TRADING_DAY"
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"
    UNVERIFIED = "UNVERIFIED"


class SessionState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MAINTENANCE = "MAINTENANCE"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True, slots=True)
class EquityCalendarConfig:
    """Explicit, version-bounded US equity calendar configuration.

    Weekdays outside the verified range fail closed. Holiday and half-day dates must
    come from a maintained authoritative calendar; this module intentionally embeds
    no guessed exchange dates.
    """

    verified_from: date
    verified_through: date
    holidays: frozenset[date] = frozenset()
    early_closes: Mapping[date, time] = field(default_factory=dict)
    timezone: str = "America/New_York"

    def __post_init__(self) -> None:
        if self.timezone != "America/New_York":
            raise ValueError("equity calendar timezone must be America/New_York")
        if self.verified_from > self.verified_through:
            raise ValueError("verified_from must not be after verified_through")
        if any(day < self.verified_from or day > self.verified_through for day in self.holidays):
            raise ValueError("holiday dates must be inside the verified calendar range")
        if any(
            day < self.verified_from or day > self.verified_through for day in self.early_closes
        ):
            raise ValueError("early-close dates must be inside the verified calendar range")
        if set(self.early_closes).intersection(self.holidays):
            raise ValueError("a date cannot be both a holiday and an early close")
        if any(day.weekday() >= 5 for day in (*self.holidays, *self.early_closes)):
            raise ValueError("weekend dates must not be configured as holidays or early closes")
        if any(close >= time(16, 0) for close in self.early_closes.values()):
            raise ValueError("an early close must be before 16:00 America/New_York")
        object.__setattr__(self, "early_closes", MappingProxyType(dict(self.early_closes)))


@dataclass(frozen=True, slots=True)
class EquitySession:
    session_date: date
    status: TradingDayStatus
    opens_at: datetime | None
    preopen_cutoff_at: datetime | None
    closes_at: datetime | None
    anchor_at: datetime | None
    is_early_close: bool = False

    @property
    def permits_trading(self) -> bool:
        return self.status is TradingDayStatus.TRADING_DAY


class USEquityCalendar:
    regular_open = time(9, 30)
    preopen_cutoff = time(9, 29, 59)
    regular_close = time(16, 0)
    anchor = time(16, 0)

    def __init__(self, config: EquityCalendarConfig) -> None:
        self._config = config

    def session(self, session_date: date) -> EquitySession:
        status = self.day_status(session_date)
        if status is not TradingDayStatus.TRADING_DAY:
            return EquitySession(session_date, status, None, None, None, None)

        close = self._config.early_closes.get(session_date, self.regular_close)
        return EquitySession(
            session_date=session_date,
            status=status,
            opens_at=self._at(session_date, self.regular_open),
            preopen_cutoff_at=self._at(session_date, self.preopen_cutoff),
            closes_at=self._at(session_date, close),
            # A half-day has no official 16:00 cash print; anchor to its actual close.
            anchor_at=self._at(session_date, close),
            is_early_close=session_date in self._config.early_closes,
        )

    def day_status(self, session_date: date) -> TradingDayStatus:
        if session_date.weekday() >= 5:
            return TradingDayStatus.WEEKEND
        if not self._in_verified_range(session_date):
            return TradingDayStatus.UNVERIFIED
        if session_date in self._config.holidays:
            return TradingDayStatus.HOLIDAY
        return TradingDayStatus.TRADING_DAY

    def _in_verified_range(self, value: date) -> bool:
        return self._config.verified_from <= value <= self._config.verified_through

    @staticmethod
    def _at(day: date, value: time) -> datetime:
        return datetime.combine(day, value, tzinfo=NEW_YORK)


@dataclass(frozen=True, slots=True)
class GlobexCalendarConfig:
    verified_from: date
    verified_through: date
    closed_dates: frozenset[date] = frozenset()
    timezone: str = "America/New_York"

    def __post_init__(self) -> None:
        if self.timezone != "America/New_York":
            raise ValueError("Globex calendar timezone must be America/New_York")
        if self.verified_from > self.verified_through:
            raise ValueError("verified_from must not be after verified_through")
        if any(
            day < self.verified_from or day > self.verified_through for day in self.closed_dates
        ):
            raise ValueError("closed dates must be inside the verified calendar range")


class GlobexSessionClock:
    """ES Globex clock with a daily 17:00-18:00 ET maintenance break.

    Friday 17:00 through Sunday 18:00 is closed. Additional exchange closures are
    explicitly configured; times outside the verified range fail closed.
    """

    maintenance_start = time(17, 0)
    maintenance_end = time(18, 0)

    def __init__(self, config: GlobexCalendarConfig) -> None:
        self._config = config

    def state_at(self, instant: datetime) -> SessionState:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("instant must be timezone-aware")
        local = instant.astimezone(NEW_YORK)
        day = local.date()
        clock = local.timetz().replace(tzinfo=None)
        if day < self._config.verified_from or day > self._config.verified_through:
            return SessionState.UNVERIFIED
        if day in self._config.closed_dates:
            return SessionState.CLOSED
        if day.weekday() == 5 or (day.weekday() == 4 and clock >= self.maintenance_start):
            return SessionState.CLOSED
        if day.weekday() == 6 and clock < self.maintenance_end:
            return SessionState.CLOSED
        if self.maintenance_start <= clock < self.maintenance_end:
            return SessionState.MAINTENANCE
        return SessionState.OPEN
