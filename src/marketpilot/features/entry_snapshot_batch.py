"""Workstream E batch driver: excursion labels → entry-feature JSONL records.

For every trading day in a window that has an excursion-label record
(``data/derived/labels/excursions.jsonl``, workstream C), this stage:

1. reads ``day`` and ``entry_price`` (the implied SPX center) from the label;
2. normalizes the full day (ES bars + 0DTE chain NBBO) via
   :func:`normalize_day`;
3. computes the entry-time feature snapshot (:func:`compute_entry_features`)
   at ``entry_time_et`` (default 09:45 ET) with time to expiry bounded by the
   16:00 ET session close;
4. appends one provenanced JSON record per day to the feature store.

Outcome taxonomy — every non-computed day is explicit, never silent:

- ``ALREADY_PRESENT``: the day already has a feature record (idempotent
  re-runs are no-ops);
- ``GAP``: the day's chain fails to load (:class:`NormalizeError` missing or
  undecodable batch, or :class:`DayStructureError` structural violation).

Days without a label record are simply absent: features are computed only for
days workstream C labelled. Early-close sessions still use 16:00 ET as the
expiry bound — the tau error is ~0.1% of a day and labelled as an
approximation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from marketpilot import __version__
from marketpilot.features.day_structure import ChainDay, DayStructureError
from marketpilot.features.entry_features import EntryFeatures
from marketpilot.features.entry_snapshot import compute_entry_features
from marketpilot.ingest.normalize import NormalizeError, normalize_day

ET = ZoneInfo("America/New_York")

#: Provenance stamp written into every feature record produced by this version.
CODE_VERSION = f"marketpilot-{__version__}:entry-snapshot-v1"

DEFAULT_LABELS_PATH = Path("data/derived/labels/excursions.jsonl")
DEFAULT_OUT_PATH = Path("data/derived/labels/entry-features.jsonl")

#: 0DTE expiry bound (ET wall clock); early closes are not special-cased (v1).
EXPIRY_CLOSE_ET = time(16, 0)

type FeatureOutcome = Literal["COMPUTED", "ALREADY_PRESENT", "GAP"]

COMPUTED: FeatureOutcome = "COMPUTED"
ALREADY_PRESENT: FeatureOutcome = "ALREADY_PRESENT"
GAP: FeatureOutcome = "GAP"

#: Injectable chain source (tests inject synthetic days; production decodes DBN).
type ChainLoader = Callable[[date], ChainDay]


@dataclass(frozen=True, slots=True)
class DayOutcome:
    """The disposition of one labelled day."""

    day: date
    outcome: FeatureOutcome
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FeatureBatchReport:
    """Aggregate result of one :func:`generate_entry_features` run."""

    start: date
    end: date
    out_path: Path
    outcomes: tuple[DayOutcome, ...]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.outcome] = counts.get(outcome.outcome, 0) + 1
        return counts


def load_label_records(path: str | Path) -> tuple[dict[str, Any], ...]:
    """Loader interface: excursion-label JSONL → raw records.

    Tests write their own label JSONL to a tmp path; the production default is
    ``data/derived/labels/excursions.jsonl``. A missing file yields no records;
    a malformed line raises :class:`ValueError` loudly.
    """

    source = Path(path)
    if not source.exists():
        return ()
    records: list[dict[str, Any]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict) or "day" not in value or "entry_price" not in value:
                raise ValueError(f"corrupt label record in {source}: {line!r}")
            records.append(value)
    return tuple(records)


class EntryFeatureStore:
    """Append-only JSONL store of per-day entry-feature records."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def computed_days(self) -> set[date]:
        if not self._path.exists():
            return set()
        days: set[date] = set()
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict) or "day" not in value:
                    raise ValueError(f"corrupt feature record in {self._path}: {line!r}")
                days.add(date.fromisoformat(str(value["day"])))
        return days

    def append(self, record: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def build_feature_record(
    features: EntryFeatures,
    computed_at: datetime,
    *,
    code_version: str = CODE_VERSION,
) -> dict[str, Any]:
    """Pure: an :class:`EntryFeatures` snapshot → its JSONL record shape.

    The dataclass fields plus ``computed_at`` (and the house provenance stamp
    ``code_version``, matching every other derived artifact in the repo).
    """

    return {
        "day": features.day.isoformat(),
        "entry_ts": features.entry_ts.isoformat(),
        "implied_center": features.implied_center,
        "atm_iv": features.atm_iv,
        "skew": features.skew,
        "realized_vol_30m": features.realized_vol_30m,
        "median_spread": features.median_spread,
        "atm_iv_valid": features.atm_iv_valid,
        "code_version": code_version,
        "computed_at": computed_at.astimezone(UTC).isoformat(),
    }


def generate_entry_features(
    *,
    start: date,
    end: date,
    data_root: str | Path,
    pit_ledger_path: str | Path,
    labels_path: str | Path = DEFAULT_LABELS_PATH,
    out_path: str | Path = DEFAULT_OUT_PATH,
    entry_time_et: time = time(9, 45),
    risk_free: float = 0.045,
    chain_loader: ChainLoader | None = None,
) -> FeatureBatchReport:
    """Compute entry features for every labelled day in ``[start, end]``.

    ``chain_loader`` is the chain source; the default decodes the landed DBN
    batches via :func:`normalize_day`. Days already present in the feature
    store are reported ``ALREADY_PRESENT`` and never recomputed or rewritten;
    days whose chain fails to load are reported ``GAP``.
    """

    if start > end:
        raise ValueError(f"start {start} must not be after end {end}")
    centers: dict[date, float] = {}
    for record in load_label_records(labels_path):
        day = date.fromisoformat(str(record["day"]))
        if start <= day <= end:
            centers[day] = float(record["entry_price"])
    store = EntryFeatureStore(out_path)
    present = store.computed_days()

    def default_loader(day: date) -> ChainDay:
        return normalize_day(
            data_root=data_root,
            pit_ledger_path=pit_ledger_path,
            day=day,
        )

    loader = chain_loader if chain_loader is not None else default_loader
    computed_at = datetime.now(UTC)
    outcomes: list[DayOutcome] = []
    for day in sorted(centers):
        if day in present:
            outcomes.append(DayOutcome(day, ALREADY_PRESENT))
            continue
        try:
            chain = loader(day)
        except (NormalizeError, DayStructureError) as exc:
            outcomes.append(DayOutcome(day, GAP, str(exc)))
            continue
        features = compute_entry_features(
            chain=chain,
            entry=datetime.combine(day, entry_time_et, tzinfo=ET),
            implied_center=centers[day],
            expiry_close=datetime.combine(day, EXPIRY_CLOSE_ET, tzinfo=ET),
            risk_free=risk_free,
        )
        store.append(build_feature_record(features, computed_at))
        present.add(day)
        outcomes.append(DayOutcome(day, COMPUTED))
    return FeatureBatchReport(
        start=start,
        end=end,
        out_path=store.path,
        outcomes=tuple(outcomes),
    )
