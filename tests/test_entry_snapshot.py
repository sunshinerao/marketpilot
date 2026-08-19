"""Hermetic tests for features.entry_snapshot + the extract-features CLI.

All chains, bars, and label files are synthetic (tmp_path); an exact
Black-Scholes pricer below generates quotes with known IVs so the inversion
can be checked against ground truth. No data/raw, no DBN, no network.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from marketpilot.cli import main
from marketpilot.features import entry_snapshot_batch
from marketpilot.features.day_structure import ChainDay, MinuteBar, OptionQuote
from marketpilot.features.entry_features import EntryFeaturesError
from marketpilot.features.entry_snapshot import (
    black_scholes_price,
    compute_entry_features,
    implied_vol,
)
from marketpilot.features.entry_snapshot_batch import (
    GAP,
    EntryFeatureStore,
    build_feature_record,
    generate_entry_features,
    load_label_records,
)
from marketpilot.ingest.normalize import NormalizeError

DAY = date(2026, 8, 17)  # Monday; 2026-08 is EDT (UTC-4)
DAY2 = date(2026, 8, 18)
ENTRY = datetime(2026, 8, 17, 13, 45, tzinfo=UTC)  # 09:45 ET
EXPIRY_CLOSE = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)  # 16:00 ET
CENTER = 6400.0
RISK_FREE = 0.045
TAU = (EXPIRY_CLOSE - ENTRY).total_seconds() / (365.25 * 86_400)


# --- exact reference Black-Scholes pricer (test-side ground truth) ----------


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs(
    *, spot: float, strike: float, rate: float, sigma: float, tau: float, is_call: bool
) -> float:
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma**2) * tau) / (sigma * math.sqrt(tau))
    d2 = d1 - sigma * math.sqrt(tau)
    if is_call:
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * tau) * _norm_cdf(d2)
    return strike * math.exp(-rate * tau) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


# --- synthetic chain builders ------------------------------------------------


def _symbol(day: date, is_call: bool, strike: float) -> str:
    flag = "C" if is_call else "P"
    return f"SPXW  {day:%y%m%d}{flag}{int(round(strike * 1000)):08d}"


def _raw_quote(
    ts: datetime,
    day: date,
    is_call: bool,
    strike: float,
    *,
    bid: float | None,
    ask: float | None,
) -> OptionQuote:
    return OptionQuote(
        ts=ts,
        symbol=_symbol(day, is_call, strike),
        bid=bid,
        ask=ask,
        bid_size=1,
        ask_size=1,
    )


def _bs_quote(
    ts: datetime,
    day: date,
    is_call: bool,
    strike: float,
    sigma: float,
    *,
    half_spread: float = 0.05,
) -> OptionQuote:
    mid = _bs(
        spot=CENTER, strike=strike, rate=RISK_FREE, sigma=sigma, tau=TAU, is_call=is_call
    )
    return _raw_quote(
        ts,
        day,
        is_call,
        strike,
        bid=max(mid - half_spread, 0.0),
        ask=mid + half_spread,
    )


def _chain_quotes(
    ts: datetime,
    day: date,
    *,
    call_sigma: float,
    put_sigma: float,
    strikes: Iterable[float] = range(6100, 6701, 5),
) -> list[OptionQuote]:
    quotes: list[OptionQuote] = []
    for strike in strikes:
        quotes.append(_bs_quote(ts, day, True, float(strike), call_sigma))
        quotes.append(_bs_quote(ts, day, False, float(strike), put_sigma))
    return sorted(quotes, key=lambda q: (q.ts, q.symbol))


def _flat_bars(day: date, *, count: int = 60, base: float = CENTER) -> tuple[MinuteBar, ...]:
    start = datetime(day.year, day.month, day.day, 13, 0, tzinfo=UTC)
    return tuple(
        MinuteBar(
            ts=start + timedelta(minutes=i),
            open=base,
            high=base,
            low=base,
            close=base,
            volume=1.0,
        )
        for i in range(count)
    )


def _chain(
    day: date,
    *,
    quotes: list[OptionQuote] | None = None,
    bars: tuple[MinuteBar, ...] | None = None,
    call_sigma: float = 0.15,
    put_sigma: float = 0.15,
) -> ChainDay:
    ts = datetime(day.year, day.month, day.day, 13, 44, tzinfo=UTC)
    resolved = (
        quotes
        if quotes is not None
        else _chain_quotes(ts, day, call_sigma=call_sigma, put_sigma=put_sigma)
    )
    return ChainDay(
        day=day,
        underlying_bars=bars if bars is not None else _flat_bars(day),
        quotes=tuple(sorted(resolved, key=lambda q: (q.ts, q.symbol))),
    )


def _features(chain: ChainDay, entry: datetime = ENTRY) -> Any:
    return compute_entry_features(
        chain=chain,
        entry=entry,
        implied_center=CENTER,
        expiry_close=EXPIRY_CLOSE,
        risk_free=RISK_FREE,
    )


# --- Black-Scholes inversion --------------------------------------------------


def test_black_scholes_price_matches_reference() -> None:
    for is_call in (True, False):
        for strike in (6300.0, 6400.0, 6500.0):
            expected = _bs(
                spot=CENTER, strike=strike, rate=RISK_FREE, sigma=0.15, tau=TAU, is_call=is_call
            )
            got = black_scholes_price(
                spot=CENTER, strike=strike, rate=RISK_FREE, sigma=0.15, tau=TAU, is_call=is_call
            )
            assert got == pytest.approx(expected, rel=1e-12, abs=1e-12)


def test_implied_vol_recovers_known_iv() -> None:
    for is_call in (True, False):
        for strike in (6275.0, 6400.0, 6525.0):
            price = _bs(
                spot=CENTER, strike=strike, rate=RISK_FREE, sigma=0.15, tau=TAU, is_call=is_call
            )
            iv = implied_vol(
                price=price, spot=CENTER, strike=strike, rate=RISK_FREE, tau=TAU, is_call=is_call
            )
            assert iv is not None
            assert iv == pytest.approx(0.15, abs=1e-6)


def test_implied_vol_rejects_unbracketed_prices() -> None:
    # Below the near-zero-sigma intrinsic floor (a stale/crossed artifact).
    assert (
        implied_vol(
            price=-0.01, spot=CENTER, strike=6400.0, rate=RISK_FREE, tau=TAU, is_call=True
        )
        is None
    )
    # Above the 500% vol ceiling.
    assert (
        implied_vol(
            price=2 * CENTER, spot=CENTER, strike=6400.0, rate=RISK_FREE, tau=TAU, is_call=True
        )
        is None
    )
    # No time left.
    assert (
        implied_vol(
            price=1.0, spot=CENTER, strike=6400.0, rate=RISK_FREE, tau=0.0, is_call=True
        )
        is None
    )


# --- compute_entry_features ---------------------------------------------------


def test_atm_iv_from_synthetic_chain() -> None:
    features = _features(_chain(DAY, call_sigma=0.15, put_sigma=0.15))
    assert features.atm_iv_valid
    assert features.atm_iv == pytest.approx(0.15, abs=1e-3)
    assert features.day == DAY
    assert features.entry_ts == ENTRY
    assert features.implied_center == CENTER
    # Symmetric chain: the ±2% legs carry the same IV.
    assert features.skew == pytest.approx(0.0, abs=1e-3)


def test_one_sided_quotes_invalidate_atm_iv() -> None:
    ts = datetime(2026, 8, 17, 13, 44, tzinfo=UTC)
    quotes = [
        _raw_quote(ts, DAY, True, 6400.0, bid=None, ask=10.5),
        _raw_quote(ts, DAY, False, 6395.0, bid=10.2, ask=None),
    ]
    features = _features(_chain(DAY, quotes=quotes))
    assert not features.atm_iv_valid
    assert features.atm_iv == 0.0
    assert features.skew == 0.0


def test_penny_quotes_invalidate_atm_iv() -> None:
    ts = datetime(2026, 8, 17, 13, 44, tzinfo=UTC)
    quotes = [
        _raw_quote(ts, DAY, True, 6400.0, bid=0.03, ask=0.05),  # mid 0.04 < 0.10
    ]
    features = _features(_chain(DAY, quotes=quotes))
    assert not features.atm_iv_valid
    assert features.atm_iv == 0.0


def test_absurd_spread_quotes_invalidate_atm_iv() -> None:
    ts = datetime(2026, 8, 17, 13, 44, tzinfo=UTC)
    # mid 2.50, spread 3.00 > 50% of mid → excluded from inversion.
    quotes = [_raw_quote(ts, DAY, True, 6400.0, bid=1.0, ask=4.0)]
    features = _features(_chain(DAY, quotes=quotes))
    assert not features.atm_iv_valid
    assert features.atm_iv == 0.0


def test_empty_chain_quotes_invalidate_atm_iv() -> None:
    ts = datetime(2026, 8, 17, 13, 44, tzinfo=UTC)
    # Only far-OTM penny contracts survive the synthetic pricer floor.
    quotes = [_raw_quote(ts, DAY, False, 5000.0, bid=0.0, ask=0.01)]
    features = _features(_chain(DAY, quotes=quotes))
    assert not features.atm_iv_valid


@pytest.mark.parametrize(
    ("call_sigma", "put_sigma", "expected_sign"),
    [(0.12, 0.20, 1), (0.20, 0.12, -1)],
)
def test_skew_sign_on_asymmetric_chain(
    call_sigma: float, put_sigma: float, expected_sign: int
) -> None:
    features = _features(_chain(DAY, call_sigma=call_sigma, put_sigma=put_sigma))
    assert features.atm_iv_valid
    assert features.skew == pytest.approx(put_sigma - call_sigma, abs=1e-3)
    assert math.copysign(1.0, features.skew) == expected_sign


def test_skew_zero_when_leg_missing_but_atm_still_valid() -> None:
    ts = datetime(2026, 8, 17, 13, 44, tzinfo=UTC)
    # Calls only: the ±2% put leg has no contract → skew 0.0, atm still valid.
    quotes = [
        _bs_quote(ts, DAY, True, float(strike), 0.15) for strike in range(6380, 6561, 5)
    ]
    features = _features(_chain(DAY, quotes=quotes))
    assert features.atm_iv_valid
    assert features.skew == 0.0


# --- realized vol --------------------------------------------------------------


def test_realized_vol_recovers_known_series() -> None:
    returns = [0.001 if i % 2 == 0 else -0.001 for i in range(29)]
    closes = [CENTER]
    for r in returns:
        closes.append(closes[-1] * math.exp(r))
    start = ENTRY - timedelta(minutes=30)
    bars = tuple(
        MinuteBar(
            ts=start + timedelta(minutes=i),
            open=closes[i],
            high=closes[i],
            low=closes[i],
            close=closes[i],
            volume=1.0,
        )
        for i in range(30)
    )
    features = _features(_chain(DAY, bars=bars))
    expected = statistics.stdev(returns) * math.sqrt(252 * 390)
    assert features.realized_vol_30m == pytest.approx(expected, rel=1e-9)


def test_realized_vol_insufficient_bars_is_zero() -> None:
    features = _features(_chain(DAY, bars=_flat_bars(DAY, count=10)))
    assert features.realized_vol_30m == 0.0


# --- median spread --------------------------------------------------------------


def test_median_spread_over_band() -> None:
    ts = datetime(2026, 8, 17, 13, 44, tzinfo=UTC)
    quotes = [
        _raw_quote(ts, DAY, True, 6300.0, bid=5.0, ask=5.10),
        _raw_quote(ts, DAY, True, 6350.0, bid=5.0, ask=5.20),
        _raw_quote(ts, DAY, False, 6450.0, bid=5.0, ask=5.30),
        # Outside the ±3% band [6208, 6592]: must be ignored.
        _raw_quote(ts, DAY, True, 6000.0, bid=0.10, ask=10.10),
    ]
    features = _features(_chain(DAY, quotes=quotes))
    assert features.median_spread == pytest.approx(0.20, abs=1e-9)


def test_median_spread_zero_without_two_sided_quotes() -> None:
    ts = datetime(2026, 8, 17, 13, 44, tzinfo=UTC)
    quotes = [_raw_quote(ts, DAY, True, 6400.0, bid=None, ask=10.5)]
    features = _features(_chain(DAY, quotes=quotes))
    assert features.median_spread == 0.0


# --- entry-minute selection ------------------------------------------------------


def test_entry_minute_selection_uses_latest_at_or_before_entry() -> None:
    # Same call at 13:40 (σ=0.40), 13:44 (σ=0.15), 13:46 (σ=0.40, after entry).
    quotes = [
        _bs_quote(datetime(2026, 8, 17, 13, 40, tzinfo=UTC), DAY, True, 6400.0, 0.40),
        _bs_quote(datetime(2026, 8, 17, 13, 44, tzinfo=UTC), DAY, True, 6400.0, 0.15),
        _bs_quote(datetime(2026, 8, 17, 13, 46, tzinfo=UTC), DAY, True, 6400.0, 0.40),
        _bs_quote(datetime(2026, 8, 17, 13, 44, tzinfo=UTC), DAY, False, 6395.0, 0.15),
    ]
    features = _features(_chain(DAY, quotes=quotes))
    assert features.atm_iv_valid
    assert features.atm_iv == pytest.approx(0.15, abs=1e-3)


def test_quote_exactly_at_entry_is_used() -> None:
    quotes = [
        _bs_quote(ENTRY, DAY, True, 6400.0, 0.25),
        _bs_quote(ENTRY, DAY, False, 6400.0, 0.25),
    ]
    features = _features(_chain(DAY, quotes=quotes))
    assert features.atm_iv_valid
    assert features.atm_iv == pytest.approx(0.25, abs=1e-3)


def test_expiry_not_after_entry_raises() -> None:
    with pytest.raises(EntryFeaturesError):
        compute_entry_features(
            chain=_chain(DAY),
            entry=ENTRY,
            implied_center=CENTER,
            expiry_close=ENTRY,
        )


# --- batch driver ---------------------------------------------------------------


def _write_labels(path: Path, records: dict[date, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for day, center in sorted(records.items()):
            handle.write(
                json.dumps(
                    {"day": day.isoformat(), "entry_price": center, "entry_et": "09:45:00"}
                )
                + "\n"
            )


def test_load_label_records_roundtrip_and_corruption(tmp_path: Path) -> None:
    labels = tmp_path / "labels.jsonl"
    _write_labels(labels, {DAY: CENTER})
    records = load_label_records(labels)
    assert len(records) == 1
    assert records[0]["day"] == DAY.isoformat()
    assert load_label_records(tmp_path / "missing.jsonl") == ()
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"not_day": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt label record"):
        load_label_records(bad)


def test_generate_entry_features_computed_gap_and_idempotency(tmp_path: Path) -> None:
    labels = tmp_path / "labels.jsonl"
    out = tmp_path / "features.jsonl"
    gap_day = DAY2
    _write_labels(labels, {DAY: CENTER, gap_day: 6410.0})

    def loader(day: date) -> ChainDay:
        if day == gap_day:
            raise NormalizeError(f"missing landed batch for {day}")
        return _chain(day)

    report = generate_entry_features(
        start=DAY,
        end=DAY2,
        data_root=tmp_path / "raw",
        pit_ledger_path=tmp_path / "pit.jsonl",
        labels_path=labels,
        out_path=out,
        chain_loader=loader,
    )
    assert report.counts() == {"COMPUTED": 1, "GAP": 1}
    gap_outcome = next(o for o in report.outcomes if o.outcome == GAP)
    assert gap_outcome.day == gap_day
    assert "missing landed batch" in gap_outcome.detail

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["day"] == DAY.isoformat()
    assert record["entry_ts"] == ENTRY.isoformat()
    assert record["implied_center"] == CENTER
    assert record["atm_iv_valid"] is True
    assert record["atm_iv"] == pytest.approx(0.15, abs=1e-3)
    assert "computed_at" in record
    assert "code_version" in record

    # Re-run: idempotent, no rewrite.
    rerun = generate_entry_features(
        start=DAY,
        end=DAY2,
        data_root=tmp_path / "raw",
        pit_ledger_path=tmp_path / "pit.jsonl",
        labels_path=labels,
        out_path=out,
        chain_loader=loader,
    )
    assert rerun.counts() == {"ALREADY_PRESENT": 1, "GAP": 1}
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1


def test_generate_entry_features_rejects_inverted_window(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be after end"):
        generate_entry_features(
            start=DAY2,
            end=DAY,
            data_root=tmp_path,
            pit_ledger_path=tmp_path / "pit.jsonl",
            labels_path=tmp_path / "labels.jsonl",
            out_path=tmp_path / "out.jsonl",
            chain_loader=_chain,
        )


def test_entry_feature_store_rejects_corrupt_record(tmp_path: Path) -> None:
    out = tmp_path / "features.jsonl"
    out.write_text('{"no_day_field": true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt feature record"):
        EntryFeatureStore(out).computed_days()


def test_build_feature_record_shape() -> None:
    features = _features(_chain(DAY))
    computed_at = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    record = build_feature_record(features, computed_at, code_version="test-v1")
    assert set(record) == {
        "day",
        "entry_ts",
        "implied_center",
        "atm_iv",
        "skew",
        "realized_vol_30m",
        "median_spread",
        "atm_iv_valid",
        "code_version",
        "computed_at",
    }
    assert record["computed_at"] == computed_at.isoformat()
    assert record["code_version"] == "test-v1"


# --- CLI -------------------------------------------------------------------------


def _cli_argv(tmp_path: Path, labels: Path, out: Path) -> list[str]:
    return [
        "extract-features",
        "--start",
        DAY.isoformat(),
        "--end",
        DAY2.isoformat(),
        "--labels",
        str(labels),
        "--out",
        str(out),
        "--data-root",
        str(tmp_path / "raw"),
        "--pit-ledger",
        str(tmp_path / "pit.jsonl"),
    ]


def test_cli_extract_features_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = tmp_path / "labels.jsonl"
    out = tmp_path / "features.jsonl"
    _write_labels(labels, {DAY: CENTER, DAY2: 6410.0})
    monkeypatch.setattr(
        entry_snapshot_batch,
        "normalize_day",
        lambda **kwargs: _chain(kwargs["day"]),
    )
    argv = _cli_argv(tmp_path, labels, out)

    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "OK"
    assert first["counts"] == {"COMPUTED": 2}
    assert first["entry"] == "09:45:00"
    assert first["out"] == str(out)

    assert main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["counts"] == {"ALREADY_PRESENT": 2}
    assert len(out.read_text(encoding="utf-8").splitlines()) == 2


def test_cli_extract_features_gap_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = tmp_path / "labels.jsonl"
    out = tmp_path / "features.jsonl"
    _write_labels(labels, {DAY: CENTER, DAY2: 6410.0})

    def fake_normalize(**kwargs: Any) -> ChainDay:
        if kwargs["day"] == DAY2:
            raise NormalizeError("missing landed batch")
        return _chain(kwargs["day"])

    monkeypatch.setattr(entry_snapshot_batch, "normalize_day", fake_normalize)
    assert main(_cli_argv(tmp_path, labels, out)) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["counts"] == {"COMPUTED": 1, "GAP": 1}


def test_cli_extract_features_rejects_bad_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = tmp_path / "labels.jsonl"
    _write_labels(labels, {DAY: CENTER})
    argv = _cli_argv(tmp_path, labels, tmp_path / "features.jsonl") + ["--entry", "not-a-time"]
    assert main(argv) == 2
    failure = json.loads(capsys.readouterr().out)
    assert failure["status"] == "FAIL"
