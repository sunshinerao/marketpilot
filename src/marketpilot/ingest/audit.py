from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from marketpilot.domain.point_in_time import PointInTimeError
from marketpilot.domain.trading_calendar import TradingDayStatus, USEquityCalendar
from marketpilot.ingest.calendar import trading_days
from marketpilot.ingest.pit_ledger import PitBatchLedger


@dataclass(frozen=True, slots=True)
class IngestAuditReport:
    scope: str
    window_start: date
    window_end: date
    expected_trading_days: int
    recorded_days: int
    missing_trading_days: tuple[date, ...]
    corrupt_records: int

    @property
    def ok(self) -> bool:
        return not self.missing_trading_days and self.corrupt_records == 0


def audit_window(
    pit_ledger: PitBatchLedger,
    calendar: USEquityCalendar,
    *,
    scope: str,
    start: date,
    end: date,
) -> IngestAuditReport:
    """Reconcile landed batch records against the versioned trading calendar."""

    expected = trading_days(calendar, start, end)
    recorded: set[date] = set()
    corrupt = 0
    for record in pit_ledger.load():
        parts = record.logical_key.split("/")
        if len(parts) != 4 or parts[2] != scope:
            continue
        try:
            record.verify()
        except PointInTimeError:
            corrupt += 1
            continue
        recorded.add(date.fromisoformat(parts[3]))
    missing = tuple(day for day in expected if day not in recorded)
    # Explicitly confirm days we skipped are explainable (holiday/weekend/unverified
    # never appear in `expected`, so anything missing here is a true gap).
    for day in missing:
        assert calendar.day_status(day) is TradingDayStatus.TRADING_DAY
    return IngestAuditReport(
        scope=scope,
        window_start=start,
        window_end=end,
        expected_trading_days=len(expected),
        recorded_days=len(recorded),
        missing_trading_days=missing,
        corrupt_records=corrupt,
    )
