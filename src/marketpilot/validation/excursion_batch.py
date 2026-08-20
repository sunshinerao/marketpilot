"""Workstream D: batch excursion-label generation over landed ES history.

For every trading day in a window that has a landed ES front-month minute
batch (``GLBX.MDP3/ohlcv-1m/es-front-month/<day>``), this stage:

1. decodes ONLY the ES batch (the 0DTE chain is never touched — labels need
   the underlying bars only);
2. builds the implied-SPX series ``S_t ~= SPX_a * F_t / F_a`` with
   ``SPX_a = anchors[prior_day]`` and ``F_a = last_bar_close(prior_day_bars)``
   where ``prior_day`` is the previous *landed* ES trading day;
3. computes the realized up/down max excursion from the entry time to the
   session close (America/New_York wall clock), honoring early closes via the
   ``early_closes`` override;
4. appends one fully-provenanced JSONL record per day to the label store.

Skip taxonomy — every non-labelled day is explicit, never silent:

- ``ALREADY_PRESENT``: the day already has a label in the store (idempotent
  re-runs are no-ops);
- ``SKIPPED_NO_PRIOR``: no earlier landed ES day exists to anchor ``F_a``;
- ``SKIPPED_NO_ANCHOR``: the prior ES day exists but ``anchors`` has no
  official SPX cash close for it;
- ``SKIPPED_COVERAGE``: the implied-SPX series cannot support an honest
  entry→close label (:class:`ExcursionCoverageError` — coverage hole);
- ``GAP``: the PIT ledger shows a definition batch for the day but no ES
  batch (an ingest gap on a trading day).

Days with neither an ES batch nor a definition batch are simply absent:
non-trading days land no batches at all. The first day of a window
legitimately hits ``SKIPPED_NO_PRIOR``/``SKIPPED_NO_ANCHOR`` — its prior day
usually precedes the window.

A corrupt or undecodable ES batch (:class:`NormalizeError`) is NOT a skip
outcome: it is a data-integrity failure at the PIT boundary and propagates,
halting the run loudly rather than silently dropping a trading day.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from marketpilot import __version__
from marketpilot.features.day_structure import MinuteBar
from marketpilot.features.implied_spx import implied_spx_series, last_bar_close
from marketpilot.ingest.normalize import normalize_es_day
from marketpilot.ingest.pit_ledger import PitBatchLedger
from marketpilot.validation.realized_excursions import (
    ExcursionCoverageError,
    realized_excursion,
)

ET = ZoneInfo("America/New_York")

#: Provenance stamp written into every label record produced by this version.
CODE_VERSION = f"marketpilot-{__version__}:excursion-batch-v1"

DEFAULT_LABELS_PATH = Path("data/derived/labels/excursions.jsonl")

_ES_LOGICAL_PREFIX = "GLBX.MDP3/ohlcv-1m/es-front-month/"
_DEFINITIONS_LOGICAL_PREFIX = "OPRA.PILLAR/definition/spxw-definitions/"

type LabelOutcome = Literal[
    "LABELLED",
    "ALREADY_PRESENT",
    "SKIPPED_NO_ANCHOR",
    "SKIPPED_NO_PRIOR",
    "SKIPPED_COVERAGE",
    "SKIPPED_ENTRY_AFTER_CLOSE",
    "GAP",
]

LABELLED: LabelOutcome = "LABELLED"
ALREADY_PRESENT: LabelOutcome = "ALREADY_PRESENT"
SKIPPED_NO_ANCHOR: LabelOutcome = "SKIPPED_NO_ANCHOR"
SKIPPED_NO_PRIOR: LabelOutcome = "SKIPPED_NO_PRIOR"
SKIPPED_COVERAGE: LabelOutcome = "SKIPPED_COVERAGE"
SKIPPED_ENTRY_AFTER_CLOSE: LabelOutcome = "SKIPPED_ENTRY_AFTER_CLOSE"
GAP: LabelOutcome = "GAP"

#: Injectable ES-bar source (tests inject synthetic bars; production decodes DBN).
type BarLoader = Callable[[date], tuple[MinuteBar, ...]]


@dataclass(frozen=True, slots=True)
class DayOutcome:
    """The disposition of one window day."""

    day: date
    outcome: LabelOutcome
    detail: str = ""


@dataclass(frozen=True, slots=True)
class LabelBatchReport:
    """Aggregate result of one :func:`generate_labels` run."""

    start: date
    end: date
    labels_path: Path
    outcomes: tuple[DayOutcome, ...]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.outcome] = counts.get(outcome.outcome, 0) + 1
        return counts


class ExcursionLabelStore:
    """Append-only JSONL store of per-day excursion labels.

    One record per day; re-appending an existing day is prevented by
    :func:`generate_labels` (the store itself stays a dumb append-only sink,
    mirroring the PIT ledger's storage discipline).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load_records(self) -> tuple[dict[str, Any], ...]:
        if not self._path.exists():
            return ()
        records: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict) or "day" not in value:
                    raise ValueError(f"corrupt label record in {self._path}: {line!r}")
                records.append(value)
        return tuple(records)

    def labelled_days(self) -> set[date]:
        return {date.fromisoformat(str(record["day"])) for record in self.load_records()}

    def append(self, record: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _ledger_day_index(
    pit_ledger_path: str | Path,
) -> tuple[dict[date, str], set[date]]:
    """Scan the PIT ledger once: ES batch record ids and definition-batch days."""

    es_records: dict[date, str] = {}
    definition_days: set[date] = set()
    for record in PitBatchLedger(pit_ledger_path).load():
        key = record.logical_key
        if key.startswith(_ES_LOGICAL_PREFIX):
            day = date.fromisoformat(key.removeprefix(_ES_LOGICAL_PREFIX))
            es_records[day] = record.record_id
        elif key.startswith(_DEFINITIONS_LOGICAL_PREFIX):
            definition_days.add(date.fromisoformat(key.removeprefix(_DEFINITIONS_LOGICAL_PREFIX)))
    return es_records, definition_days


def build_label_record(
    *,
    day: date,
    day_bars: tuple[MinuteBar, ...],
    prior_bars: tuple[MinuteBar, ...],
    prior_cash_close: float,
    entry_time_et: time,
    close_time_et: time,
    es_record_id: str,
    prior_es_record_id: str,
    computed_at: datetime,
    code_version: str = CODE_VERSION,
) -> dict[str, Any]:
    """Pure: one day's ES bars + anchor inputs → a provenanced label record.

    The implied-SPX series is built from ``day_bars`` with
    ``SPX_a = prior_cash_close`` and ``F_a = last_bar_close(prior_bars)``; the
    excursion window runs from ``entry_time_et`` to ``close_time_et``
    (America/New_York wall clock). Because the implied series carries one
    price per minute bar close, the extremes are over implied minute closes,
    not intraminute highs/lows — an explicitly labelled approximation.

    Raises :class:`ExcursionCoverageError` (coverage hole) and
    :class:`ImpliedSpxError` (anchor contract violation); callers map the
    former to ``SKIPPED_COVERAGE``.
    """

    f_a = last_bar_close(prior_bars)
    series = implied_spx_series(
        day_bars,
        prior_cash_close=prior_cash_close,
        anchor_futures_price=f_a,
    )
    entry = datetime.combine(day, entry_time_et, tzinfo=ET)
    close = datetime.combine(day, close_time_et, tzinfo=ET)
    result = realized_excursion(series, entry=entry, close=close)
    return {
        "day": day.isoformat(),
        "entry_et": entry_time_et.isoformat(),
        "close_et": close_time_et.isoformat(),
        "spx_a": prior_cash_close,
        "f_a": f_a,
        "entry_price": result.entry_price,
        "close_price": result.close_price,
        "up_max": result.up_max,
        "down_max": result.down_max,
        "up_max_ts": result.up_max_ts.isoformat(),
        "down_max_ts": result.down_max_ts.isoformat(),
        "sample_count": result.sample_count,
        "es_record_id": es_record_id,
        "prior_es_record_id": prior_es_record_id,
        "code_version": code_version,
        "computed_at": computed_at.astimezone(UTC).isoformat(),
    }


def generate_labels(
    *,
    data_root: str | Path,
    pit_ledger_path: str | Path,
    anchors: dict[date, float],
    start: date,
    end: date,
    entry_time_et: time = time(9, 45),
    close_time_et: time = time(16, 0),
    early_closes: dict[date, time] | None = None,
    labels_path: str | Path = DEFAULT_LABELS_PATH,
    bar_loader: BarLoader | None = None,
) -> LabelBatchReport:
    """Generate excursion labels for every landed ES day in ``[start, end]``.

    ``anchors`` maps trading days to official SPX cash closes (see
    ``load_anchor_closes``); each day's label anchors on the *previous landed
    ES day*, which may precede ``start``. ``early_closes`` overrides the
    session close (ET) for half-day sessions. ``bar_loader`` is the ES-bar
    source; the default decodes the landed DBN batch via
    :func:`normalize_es_day`. Days already present in the label store are
    reported ``ALREADY_PRESENT`` and never recomputed or rewritten.
    """

    if start > end:
        raise ValueError(f"start {start} must not be after end {end}")
    es_records, definition_days = _ledger_day_index(pit_ledger_path)
    es_days = sorted(es_records)
    store = ExcursionLabelStore(labels_path)
    present = store.labelled_days()
    early = early_closes if early_closes is not None else {}

    def default_loader(day: date) -> tuple[MinuteBar, ...]:
        return normalize_es_day(
            data_root=data_root,
            pit_ledger_path=pit_ledger_path,
            day=day,
        )

    loader = bar_loader if bar_loader is not None else default_loader
    bars_cache: dict[date, tuple[MinuteBar, ...]] = {}

    def bars_for(day: date) -> tuple[MinuteBar, ...]:
        # Each ES batch is decoded at most once per run: a day's bars serve
        # both its own label and the next day's F_a anchor.
        if day not in bars_cache:
            bars_cache[day] = loader(day)
        return bars_cache[day]

    computed_at = datetime.now(UTC)
    outcomes: list[DayOutcome] = []
    day = start
    while day <= end:
        if day not in es_records:
            if day in definition_days:
                outcomes.append(
                    DayOutcome(day, GAP, "definition batch landed but ES batch is missing")
                )
            # Otherwise the day is absent: a non-trading day (or a day the
            # ingest never ran for) lands no batches at all.
            day += timedelta(days=1)
            continue
        if day in present:
            outcomes.append(DayOutcome(day, ALREADY_PRESENT))
            day += timedelta(days=1)
            continue
        prior_index = bisect_left(es_days, day) - 1
        if prior_index < 0:
            outcomes.append(
                DayOutcome(day, SKIPPED_NO_PRIOR, "no earlier landed ES day to anchor F_a")
            )
            day += timedelta(days=1)
            continue
        prior_day = es_days[prior_index]
        if prior_day not in anchors:
            outcomes.append(
                DayOutcome(
                    day,
                    SKIPPED_NO_ANCHOR,
                    f"no official SPX cash close for prior day {prior_day}",
                )
            )
            day += timedelta(days=1)
            continue
        day_close = early.get(day, close_time_et)
        if entry_time_et >= day_close:
            # e.g. a 13:00 entry on a 13:00 early-close day: no excursion
            # window exists; skip explicitly instead of failing the batch.
            outcomes.append(
                DayOutcome(
                    day,
                    SKIPPED_ENTRY_AFTER_CLOSE,
                    f"entry {entry_time_et} is not before close {day_close}",
                )
            )
            day += timedelta(days=1)
            continue
        try:
            record = build_label_record(
                day=day,
                day_bars=bars_for(day),
                prior_bars=bars_for(prior_day),
                prior_cash_close=anchors[prior_day],
                entry_time_et=entry_time_et,
                close_time_et=day_close,
                es_record_id=es_records[day],
                prior_es_record_id=es_records[prior_day],
                computed_at=computed_at,
            )
        except ExcursionCoverageError as exc:
            outcomes.append(DayOutcome(day, SKIPPED_COVERAGE, str(exc)))
        else:
            store.append(record)
            present.add(day)
            outcomes.append(DayOutcome(day, LABELLED))
        day += timedelta(days=1)
    return LabelBatchReport(
        start=start,
        end=end,
        labels_path=store.path,
        outcomes=tuple(outcomes),
    )
