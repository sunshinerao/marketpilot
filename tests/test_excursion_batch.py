"""Hermetic tests for validation.excursion_batch.

Synthetic typed ES bars + canned anchors + a real PIT ledger on tmp_path;
the DBN decode path is replaced by an injected ``bar_loader``, so no DBN,
no data/raw, and no network are involved.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from marketpilot.domain.point_in_time import PointInTimeRecord
from marketpilot.features.day_structure import MinuteBar
from marketpilot.ingest.normalize import es_logical_key
from marketpilot.ingest.pit_ledger import PitBatchLedger
from marketpilot.validation.excursion_batch import (
    ALREADY_PRESENT,
    CODE_VERSION,
    GAP,
    LABELLED,
    SKIPPED_COVERAGE,
    SKIPPED_NO_ANCHOR,
    SKIPPED_NO_PRIOR,
    ExcursionLabelStore,
    LabelBatchReport,
    build_label_record,
    generate_labels,
)

PRIOR = date(2026, 8, 14)  # Friday
DAY = date(2026, 8, 17)  # Monday
GAP_DAY = date(2026, 8, 18)  # Tuesday

# 2026-08 is EDT (UTC-4): 09:30 ET = 13:30 UTC, 09:45 ET = 13:45 UTC,
# 16:00 ET = 20:00 UTC, 13:00 ET = 17:00 UTC.
RTH_OPEN_UTC = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)
RTH_CLOSE_UTC = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)
EARLY_CLOSE_UTC = datetime(2026, 8, 17, 17, 0, tzinfo=UTC)


def _record(key: str, seen: datetime) -> PointInTimeRecord:
    return PointInTimeRecord.create(
        logical_key=key,
        published_at=seen,
        first_seen_at=seen,
        provider="databento",
        provider_version="historical-v1",
        schema_version="dbn-v2",
        content={"object_key": f"objects/{key}", "dataset": "GLBX.MDP3", "schema": "ohlcv-1m"},
    )


def _write_ledger(root: Path, keys: tuple[str, ...]) -> Path:
    path = root / "pit" / "records.jsonl"
    ledger = PitBatchLedger(path)
    seen = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    for index, key in enumerate(keys):
        ledger.append(_record(key, seen + timedelta(minutes=index)))
    return path


def _definitions_key(day: date) -> str:
    return f"OPRA.PILLAR/definition/spxw-definitions/{day.isoformat()}"


def _bars(
    start: datetime, end: datetime, *, base: float, drift: float = 1.0
) -> tuple[MinuteBar, ...]:
    """Minute bars from ``start`` to ``end`` inclusive; close ramps linearly."""

    bars: list[MinuteBar] = []
    ts = start
    index = 0
    while ts <= end:
        close = base + drift * index
        bars.append(
            MinuteBar(
                ts=ts, open=close, high=close + 0.25, low=close - 0.25, close=close, volume=100.0
            )
        )
        ts += timedelta(minutes=1)
        index += 1
    return tuple(bars)


def _prior_bars() -> tuple[MinuteBar, ...]:
    # Six bars 19:55–20:00 UTC, closes 6395..6400 → F_a = 6400.
    start = datetime(2026, 8, 14, 19, 55, tzinfo=UTC)
    end = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)
    return _bars(start, end, base=6395.0)


def _day_bars() -> tuple[MinuteBar, ...]:
    # 391 bars 13:30–20:00 UTC, closes 6400..6790.
    return _bars(RTH_OPEN_UTC, RTH_CLOSE_UTC, base=6400.0)


def _generate(
    root: Path,
    *,
    keys: tuple[str, ...],
    bars: dict[date, tuple[MinuteBar, ...]],
    anchors: dict[date, float],
    start: date,
    end: date,
    labels_name: str = "labels.jsonl",
    early_closes: dict[date, time] | None = None,
) -> tuple[Path, LabelBatchReport]:
    pit_path = _write_ledger(root, keys)
    labels_path = root / "derived" / labels_name
    report = generate_labels(
        data_root=root / "raw",
        pit_ledger_path=pit_path,
        anchors=anchors,
        start=start,
        end=end,
        early_closes=early_closes,
        labels_path=labels_path,
        bar_loader=bars.__getitem__,
    )
    return labels_path, report


def _outcome_map(report: LabelBatchReport) -> dict[date, str]:
    return {outcome.day: outcome.outcome for outcome in report.outcomes}


def test_happy_path_labels_day_with_full_provenance(tmp_path: Path) -> None:
    keys = (es_logical_key(PRIOR), es_logical_key(DAY))
    labels_path, report = _generate(
        tmp_path,
        keys=keys,
        bars={PRIOR: _prior_bars(), DAY: _day_bars()},
        anchors={PRIOR: 6400.0},
        start=DAY,
        end=DAY,
    )

    assert _outcome_map(report) == {DAY: LABELLED}
    records = ExcursionLabelStore(labels_path).load_records()
    assert len(records) == 1
    record = records[0]
    assert record["day"] == "2026-08-17"
    assert record["entry_et"] == "09:45:00"
    assert record["close_et"] == "16:00:00"
    # S_a = 6400 and F_a = 6400 → ratio 1: implied closes equal ES closes.
    assert record["spx_a"] == 6400.0
    assert record["f_a"] == 6400.0
    assert record["entry_price"] == 6415.0  # 13:45 UTC bar close
    assert record["close_price"] == 6790.0  # 20:00 UTC bar close
    assert record["up_max"] == 375.0
    assert record["down_max"] == 0.0
    assert record["up_max_ts"] == "2026-08-17T20:00:00+00:00"
    assert record["down_max_ts"] == "2026-08-17T13:46:00+00:00"
    assert record["sample_count"] == 375  # bars in (13:45, 20:00] UTC
    # Provenance: the label names the exact PIT batches and code it came from.
    ledger = {r.logical_key: r for r in PitBatchLedger(tmp_path / "pit" / "records.jsonl").load()}
    assert record["es_record_id"] == ledger[es_logical_key(DAY)].record_id
    assert record["prior_es_record_id"] == ledger[es_logical_key(PRIOR)].record_id
    assert record["code_version"] == CODE_VERSION
    computed_at = datetime.fromisoformat(str(record["computed_at"]))
    assert computed_at.tzinfo is not None


def test_skipped_no_anchor_is_explicit(tmp_path: Path) -> None:
    labels_path, report = _generate(
        tmp_path,
        keys=(es_logical_key(PRIOR), es_logical_key(DAY)),
        bars={PRIOR: _prior_bars(), DAY: _day_bars()},
        anchors={},  # no official close for the prior day
        start=DAY,
        end=DAY,
    )

    assert _outcome_map(report) == {DAY: SKIPPED_NO_ANCHOR}
    assert not labels_path.exists()  # no label was written


def test_skipped_no_prior_is_explicit(tmp_path: Path) -> None:
    labels_path, report = _generate(
        tmp_path,
        keys=(es_logical_key(DAY),),  # no earlier landed ES day exists
        bars={DAY: _day_bars()},
        anchors={PRIOR: 6400.0},
        start=DAY,
        end=DAY,
    )

    assert _outcome_map(report) == {DAY: SKIPPED_NO_PRIOR}
    assert not labels_path.exists()


def test_first_window_day_skips_prior_then_later_days_label(tmp_path: Path) -> None:
    """The first day of a window legitimately hits SKIPPED_NO_PRIOR."""

    labels_path, report = _generate(
        tmp_path,
        keys=(es_logical_key(PRIOR), es_logical_key(DAY)),
        bars={PRIOR: _prior_bars(), DAY: _day_bars()},
        anchors={PRIOR: 6400.0},
        start=PRIOR,
        end=DAY,
    )

    assert _outcome_map(report) == {PRIOR: SKIPPED_NO_PRIOR, DAY: LABELLED}
    assert len(ExcursionLabelStore(labels_path).load_records()) == 1


def test_skipped_coverage_is_explicit(tmp_path: Path) -> None:
    # Bars stop at 19:00 UTC — an hour before the 16:00 ET close.
    truncated = _bars(RTH_OPEN_UTC, datetime(2026, 8, 17, 19, 0, tzinfo=UTC), base=6400.0)
    labels_path, report = _generate(
        tmp_path,
        keys=(es_logical_key(PRIOR), es_logical_key(DAY)),
        bars={PRIOR: _prior_bars(), DAY: truncated},
        anchors={PRIOR: 6400.0},
        start=DAY,
        end=DAY,
    )

    outcomes = {outcome.day: outcome for outcome in report.outcomes}
    assert outcomes[DAY].outcome == SKIPPED_COVERAGE
    assert "coverage" in outcomes[DAY].detail
    assert not labels_path.exists()


def test_gap_recorded_only_when_definition_landed_without_es(tmp_path: Path) -> None:
    """Weekend days (no batches at all) are absent; a definition batch without
    the ES batch on a trading day is an explicit GAP."""

    keys = (es_logical_key(PRIOR), es_logical_key(DAY), _definitions_key(GAP_DAY))
    _, report = _generate(
        tmp_path,
        keys=keys,
        bars={PRIOR: _prior_bars(), DAY: _day_bars()},
        anchors={PRIOR: 6400.0},
        start=date(2026, 8, 15),  # Saturday — no batches, must be absent
        end=GAP_DAY,
    )

    assert _outcome_map(report) == {DAY: LABELLED, GAP_DAY: GAP}


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    keys = (es_logical_key(PRIOR), es_logical_key(DAY))
    bars = {PRIOR: _prior_bars(), DAY: _day_bars()}
    anchors = {PRIOR: 6400.0}
    labels_path, first = _generate(
        tmp_path, keys=keys, bars=bars, anchors=anchors, start=DAY, end=DAY
    )
    first_records = ExcursionLabelStore(labels_path).load_records()

    _, second = _generate(
        tmp_path, keys=keys, bars=bars, anchors=anchors, start=DAY, end=DAY
    )

    assert _outcome_map(first) == {DAY: LABELLED}
    assert _outcome_map(second) == {DAY: ALREADY_PRESENT}
    # The store is untouched: same single record, same computed_at stamp.
    assert ExcursionLabelStore(labels_path).load_records() == first_records


def test_early_close_session_uses_actual_close(tmp_path: Path) -> None:
    half_day = _bars(RTH_OPEN_UTC, EARLY_CLOSE_UTC, base=6400.0)
    keys = (es_logical_key(PRIOR), es_logical_key(DAY))
    bars = {PRIOR: _prior_bars(), DAY: half_day}
    anchors = {PRIOR: 6400.0}

    # With the default 16:00 ET close, a half-day session cannot be labelled.
    full_labels, full_report = _generate(
        tmp_path,
        keys=keys,
        bars=bars,
        anchors=anchors,
        start=DAY,
        end=DAY,
        labels_name="full.jsonl",
    )
    assert _outcome_map(full_report) == {DAY: SKIPPED_COVERAGE}
    assert not full_labels.exists()

    # With the session's actual 13:00 ET close it labels cleanly.
    early_labels, early_report = _generate(
        tmp_path,
        keys=keys,
        bars=bars,
        anchors=anchors,
        start=DAY,
        end=DAY,
        labels_name="early.jsonl",
        early_closes={DAY: time(13, 0)},
    )
    assert _outcome_map(early_report) == {DAY: LABELLED}
    record = ExcursionLabelStore(early_labels).load_records()[0]
    assert record["close_et"] == "13:00:00"
    assert record["close_price"] == 6610.0  # 17:00 UTC bar close
    assert record["sample_count"] == 195  # bars in (13:45, 17:00] UTC


def test_start_after_end_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be after end"):
        generate_labels(
            data_root=tmp_path / "raw",
            pit_ledger_path=tmp_path / "pit" / "records.jsonl",
            anchors={},
            start=DAY,
            end=PRIOR,
            labels_path=tmp_path / "labels.jsonl",
            bar_loader=lambda day: (),
        )


def test_build_label_record_is_pure_and_provenanced() -> None:
    computed_at = datetime(2026, 8, 21, 9, 30, tzinfo=UTC)
    record = build_label_record(
        day=DAY,
        day_bars=_day_bars(),
        prior_bars=_prior_bars(),
        prior_cash_close=3200.0,  # S_a/F_a ratio 0.5 scales the implied series
        entry_time_et=time(9, 45),
        close_time_et=time(16, 0),
        es_record_id="es-rec",
        prior_es_record_id="prior-rec",
        computed_at=computed_at,
    )

    assert record["entry_price"] == pytest.approx(6415.0 / 2)
    assert record["up_max"] == pytest.approx((6790.0 - 6415.0) / 2)
    assert record["down_max"] == 0.0
    assert record["spx_a"] == 3200.0
    assert record["f_a"] == 6400.0
    assert record["es_record_id"] == "es-rec"
    assert record["prior_es_record_id"] == "prior-rec"
    assert record["code_version"] == CODE_VERSION
    assert record["computed_at"] == computed_at.isoformat()
    assert json.loads(json.dumps(record)) == record  # JSON-round-trippable
