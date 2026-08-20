"""Hermetic tests for validation/condor_economics.py — synthetic chains only.

No data/raw, no network, no real label or distances files: every chain is a
hand-built :class:`ChainDay` with known NBBO quotes, and batch tests write
their own JSONL inputs into ``tmp_path``.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from marketpilot.cli import main
from marketpilot.features.day_structure import ChainDay, MinuteBar, OptionQuote
from marketpilot.ingest.normalize import NormalizeError
from marketpilot.models.strikepilot.strikes import IronCondorStrikes
from marketpilot.validation.condor_economics import (
    CondorEconomicsError,
    DayEconomics,
    PricingStatus,
    evaluate_day,
    load_excursion_labels,
    load_tail_distances,
    osi_symbol,
    price_condor,
    run_economics_batch,
    settle_condor,
    summarize,
)
from marketpilot.validation.tail_distances import TailDistances

DAY = date(2026, 8, 17)  # Monday; August is EDT, so 09:45 ET == 13:45 UTC
ENTRY = datetime(2026, 8, 17, 13, 45, tzinfo=UTC)
QUOTE_TS = datetime(2026, 8, 17, 13, 44, tzinfo=UTC)

STRIKES = IronCondorStrikes(
    short_put=6400,
    long_put=6395,
    short_call=6450,
    long_call=6455,
    put_distance=41.0,
    call_distance=9.0,
)

# Known-entry-fill quotes: every leg has bid != ask so the conservative side
# selection (shorts at bid, longs at ask) is observable in the fill.
LEG_QUOTES = {
    (6400, "P"): (1.50, 1.70),  # short put: sells at bid 1.50
    (6395, "P"): (0.50, 0.60),  # long put: buys at ask 0.60
    (6450, "C"): (1.40, 1.60),  # short call: sells at bid 1.40
    (6455, "C"): (0.40, 0.50),  # long call: buys at ask 0.50
}
EXPECTED_CREDIT = 1.50 + 1.40 - 0.60 - 0.50  # 1.80


def _symbol(strike: int, right: str, day: date = DAY) -> str:
    return f"SPXW  {day:%y%m%d}{right}{strike * 1000:08d}"


def _quote(
    strike: int,
    right: str,
    bid: float | None,
    ask: float | None,
    *,
    ts: datetime = QUOTE_TS,
    day: date = DAY,
) -> OptionQuote:
    return OptionQuote(
        ts=ts,
        symbol=_symbol(strike, right, day),
        bid=bid,
        ask=ask,
        bid_size=10,
        ask_size=10,
    )


def _bar(ts: datetime = QUOTE_TS) -> MinuteBar:
    return MinuteBar(ts=ts, open=6440.0, high=6442.0, low=6438.0, close=6441.0, volume=100.0)


def _chain(
    quotes: list[OptionQuote],
    *,
    day: date = DAY,
) -> ChainDay:
    ordered = sorted(quotes, key=lambda quote: (quote.ts, quote.symbol))
    return ChainDay(day=day, underlying_bars=(_bar(),), quotes=tuple(ordered))


def _standard_quotes() -> list[OptionQuote]:
    return [
        _quote(strike, right, bid, ask)
        for (strike, right), (bid, ask) in LEG_QUOTES.items()
    ]


def _distances(day: date = DAY, regime: str = "LOW_VOL") -> TailDistances:
    # center 6441, down 40 -> short_put 6400; up 8 -> short_call 6450.
    return TailDistances(
        day=day,
        down_distance=40.0,
        up_distance=8.0,
        regime=regime,
        model_version="tail-model-test",
        quantile=0.95,
    )


def _priced_day(
    day: date,
    pnl: float,
    *,
    regime: str = "LOW_VOL",
    credit: float = 1.8,
) -> DayEconomics:
    return DayEconomics(
        day=day,
        regime=regime,
        status=PricingStatus.PRICED,
        credit=credit,
        max_loss=5.0 - credit,
        settlement_loss=credit - pnl,
        pnl=pnl,
        put_breached=pnl < credit - 5.0,
        call_breached=False,
    )


# --- symbol + entry fill -------------------------------------------------


def test_osi_symbol_is_padded_21_char_form() -> None:
    assert osi_symbol(day=DAY, right="P", strike=6400) == "SPXW  260817P06400000"
    assert len(osi_symbol(day=DAY, right="C", strike=6455)) == 21
    with pytest.raises(CondorEconomicsError, match="right"):
        osi_symbol(day=DAY, right="X", strike=6400)


def test_price_condor_exact_credit_at_conservative_sides() -> None:
    fill = price_condor(chain=_chain(_standard_quotes()), entry=ENTRY, strikes=STRIKES)
    assert fill is not None
    assert fill.filled_at == ENTRY
    # Shorts fill at the bid, longs at the ask — never the midpoint.
    assert fill.short_put_price == 1.50
    assert fill.short_call_price == 1.40
    assert fill.long_put_price == 0.60
    assert fill.long_call_price == 0.50
    assert fill.credit == pytest.approx(EXPECTED_CREDIT)


def test_price_condor_uses_latest_quote_at_or_before_entry() -> None:
    early = [
        _quote(strike, right, 0.01, 0.02, ts=QUOTE_TS - timedelta(minutes=1))
        for (strike, right) in LEG_QUOTES
    ]
    after = [
        _quote(strike, right, 9.99, 9.99, ts=ENTRY + timedelta(minutes=1))
        for (strike, right) in LEG_QUOTES
    ]
    fill = price_condor(
        chain=_chain(early + _standard_quotes() + after), entry=ENTRY, strikes=STRIKES
    )
    assert fill is not None
    assert fill.credit == pytest.approx(EXPECTED_CREDIT)


def test_price_condor_accepts_quote_exactly_at_entry() -> None:
    at_entry = [
        _quote(strike, right, bid, ask, ts=ENTRY)
        for (strike, right), (bid, ask) in LEG_QUOTES.items()
    ]
    fill = price_condor(chain=_chain(at_entry), entry=ENTRY, strikes=STRIKES)
    assert fill is not None
    assert fill.credit == pytest.approx(EXPECTED_CREDIT)


def test_price_condor_missing_leg_is_unpriceable() -> None:
    quotes = [q for q in _standard_quotes() if q.symbol != _symbol(6455, "C")]
    assert price_condor(chain=_chain(quotes), entry=ENTRY, strikes=STRIKES) is None


def test_price_condor_missing_side_is_unpriceable() -> None:
    no_long_ask = [
        _quote(6395, "P", 0.50, None) if (s, r) == (6395, "P") else _quote(s, r, b, a)
        for (s, r), (b, a) in LEG_QUOTES.items()
    ]
    assert price_condor(chain=_chain(no_long_ask), entry=ENTRY, strikes=STRIKES) is None
    no_short_bid = [
        _quote(6400, "P", None, 1.70) if (s, r) == (6400, "P") else _quote(s, r, b, a)
        for (s, r), (b, a) in LEG_QUOTES.items()
    ]
    assert price_condor(chain=_chain(no_short_bid), entry=ENTRY, strikes=STRIKES) is None


def test_price_condor_zero_bid_short_leg_is_unpriceable() -> None:
    quotes = [
        _quote(6450, "C", 0.0, 1.60) if (s, r) == (6450, "C") else _quote(s, r, b, a)
        for (s, r), (b, a) in LEG_QUOTES.items()
    ]
    assert price_condor(chain=_chain(quotes), entry=ENTRY, strikes=STRIKES) is None


def test_price_condor_requires_quotes_at_or_before_entry() -> None:
    late = [
        _quote(strike, right, bid, ask, ts=ENTRY + timedelta(minutes=1))
        for (strike, right), (bid, ask) in LEG_QUOTES.items()
    ]
    assert price_condor(chain=_chain(late), entry=ENTRY, strikes=STRIKES) is None


def test_price_condor_rejects_naive_entry() -> None:
    with pytest.raises(CondorEconomicsError, match="timezone-aware"):
        price_condor(
            chain=_chain(_standard_quotes()),
            entry=datetime(2026, 8, 17, 13, 45),
            strikes=STRIKES,
        )


# --- settlement ----------------------------------------------------------


def test_settle_condor_inside_corridor_is_zero_loss() -> None:
    assert settle_condor(STRIKES, 6400.0) == 0.0
    assert settle_condor(STRIKES, 6450.0) == 0.0
    assert settle_condor(STRIKES, 6423.5) == 0.0


def test_settle_condor_through_short_put_is_partial_loss() -> None:
    assert settle_condor(STRIKES, 6398.0) == pytest.approx(2.0)
    assert settle_condor(STRIKES, 6395.0) == pytest.approx(5.0)


def test_settle_condor_through_long_put_is_capped_at_wing() -> None:
    assert settle_condor(STRIKES, 6390.0) == pytest.approx(5.0)
    assert settle_condor(STRIKES, 6000.0) == pytest.approx(5.0)


def test_settle_condor_call_side_mirrors_put_side() -> None:
    assert settle_condor(STRIKES, 6453.0) == pytest.approx(3.0)
    assert settle_condor(STRIKES, 6455.0) == pytest.approx(5.0)
    assert settle_condor(STRIKES, 9999.0) == pytest.approx(5.0)


def test_settle_condor_rejects_bad_inputs() -> None:
    with pytest.raises(CondorEconomicsError, match="positive"):
        settle_condor(STRIKES, 0.0)
    with pytest.raises(CondorEconomicsError, match="finite"):
        settle_condor(STRIKES, float("nan"))
    asymmetric = IronCondorStrikes(
        short_put=6400,
        long_put=6395,
        short_call=6450,
        long_call=6460,
        put_distance=41.0,
        call_distance=9.0,
    )
    with pytest.raises(CondorEconomicsError, match="symmetric"):
        settle_condor(asymmetric, 6400.0)


# --- evaluate_day --------------------------------------------------------


def test_evaluate_day_priced_inside_corridor() -> None:
    result = evaluate_day(
        chain=_chain(_standard_quotes()),
        entry=ENTRY,
        center=6441.0,
        distances=_distances(),
        close_price=6420.0,
    )
    assert result.status is PricingStatus.PRICED
    assert result.day == DAY
    assert result.regime == "LOW_VOL"
    assert result.credit == pytest.approx(EXPECTED_CREDIT)
    assert result.max_loss == pytest.approx(5.0 - EXPECTED_CREDIT)
    assert result.settlement_loss == 0.0
    assert result.pnl == pytest.approx(EXPECTED_CREDIT)
    assert result.put_breached is False
    assert result.call_breached is False


def test_evaluate_day_pnl_arithmetic_through_long_put() -> None:
    result = evaluate_day(
        chain=_chain(_standard_quotes()),
        entry=ENTRY,
        center=6441.0,
        distances=_distances(),
        close_price=6390.0,
    )
    assert result.status is PricingStatus.PRICED
    assert result.settlement_loss == pytest.approx(5.0)
    assert result.pnl == pytest.approx(EXPECTED_CREDIT - 5.0)
    assert result.put_breached is True
    assert result.call_breached is False


def test_evaluate_day_call_breach_flag() -> None:
    result = evaluate_day(
        chain=_chain(_standard_quotes()),
        entry=ENTRY,
        center=6441.0,
        distances=_distances(),
        close_price=6453.0,
    )
    assert result.settlement_loss == pytest.approx(3.0)
    assert result.pnl == pytest.approx(EXPECTED_CREDIT - 3.0)
    assert result.call_breached is True
    assert result.put_breached is False


def test_evaluate_day_unpriceable_suppresses_numerics() -> None:
    quotes = [q for q in _standard_quotes() if q.symbol != _symbol(6395, "P")]
    result = evaluate_day(
        chain=_chain(quotes),
        entry=ENTRY,
        center=6441.0,
        distances=_distances(),
        close_price=6420.0,
    )
    assert result.status is PricingStatus.UNPRICEABLE
    assert result.credit is None
    assert result.max_loss is None
    assert result.settlement_loss is None
    assert result.pnl is None
    assert result.put_breached is None
    assert result.call_breached is None


def test_evaluate_day_rejects_distances_chain_mismatch() -> None:
    with pytest.raises(CondorEconomicsError, match="does not match"):
        evaluate_day(
            chain=_chain(_standard_quotes()),
            entry=ENTRY,
            center=6441.0,
            distances=_distances(day=date(2026, 8, 18)),
            close_price=6420.0,
        )


def test_day_economics_contract_is_fail_closed() -> None:
    with pytest.raises(CondorEconomicsError, match="requires pnl"):
        DayEconomics(
            day=DAY,
            regime="LOW_VOL",
            status=PricingStatus.PRICED,
            credit=1.8,
            max_loss=3.2,
            settlement_loss=0.0,
            put_breached=False,
            call_breached=False,
        )
    with pytest.raises(CondorEconomicsError, match="must not carry credit"):
        DayEconomics(day=DAY, regime="LOW_VOL", status=PricingStatus.UNPRICEABLE, credit=1.0)
    with pytest.raises(CondorEconomicsError, match="credit minus settlement_loss"):
        DayEconomics(
            day=DAY,
            regime="LOW_VOL",
            status=PricingStatus.PRICED,
            credit=1.8,
            max_loss=3.2,
            settlement_loss=0.0,
            pnl=1.0,
            put_breached=False,
            call_breached=False,
        )


# --- summarize -----------------------------------------------------------


def _summary_series() -> list[DayEconomics]:
    days: list[DayEconomics] = []
    base = date(2026, 8, 3)
    for index in range(10):  # LOW_VOL: 10 winners at +1.0
        days.append(_priced_day(base + timedelta(days=index), 1.0, regime="LOW_VOL"))
    for index in range(9):  # HIGH_VOL: 9 winners at +1.0
        days.append(
            _priced_day(base + timedelta(days=10 + index), 1.0, regime="HIGH_VOL")
        )
    # HIGH_VOL: one max-loss day at -4.0 (credit 1.0, settlement 5.0).
    days.append(_priced_day(base + timedelta(days=19), -4.0, regime="HIGH_VOL", credit=1.0))
    days.append(
        DayEconomics(
            day=base + timedelta(days=20),
            regime="LOW_VOL",
            status=PricingStatus.UNPRICEABLE,
        )
    )
    days.append(
        DayEconomics(
            day=base + timedelta(days=21),
            regime="HIGH_VOL",
            status=PricingStatus.UNPRICEABLE,
        )
    )
    return days


def test_summarize_ev_cvar_and_counts() -> None:
    summary = summarize(_summary_series())
    assert summary.n_priced == 20
    assert summary.n_unpriceable == 2
    assert summary.total_pnl == pytest.approx(15.0)  # 19 * 1.0 - 4.0
    assert summary.mean_pnl == pytest.approx(0.75)
    # EV is per candidate day: unpriceable days count as zero-PnL no-trades.
    assert summary.ev == pytest.approx(15.0 / 22)
    # Worst 5% of 20 priced days is the single worst day.
    assert summary.cvar_95 == pytest.approx(-4.0)
    assert summary.max_daily_loss == pytest.approx(4.0)
    assert summary.win_rate == pytest.approx(0.95)


def test_summarize_regime_breakdown() -> None:
    summary = summarize(_summary_series())
    assert set(summary.regimes) == {"LOW_VOL", "HIGH_VOL"}
    low = summary.regimes["LOW_VOL"]
    assert low.n_priced == 10
    assert low.n_unpriceable == 1
    assert low.total_pnl == pytest.approx(10.0)
    assert low.mean_pnl == pytest.approx(1.0)
    assert low.win_rate == pytest.approx(1.0)
    assert low.max_daily_loss == pytest.approx(0.0)
    high = summary.regimes["HIGH_VOL"]
    assert high.n_priced == 10
    assert high.n_unpriceable == 1
    assert high.total_pnl == pytest.approx(5.0)
    assert high.mean_pnl == pytest.approx(0.5)
    assert high.win_rate == pytest.approx(0.9)
    assert high.max_daily_loss == pytest.approx(4.0)


def test_summarize_without_priced_days_reports_nulls() -> None:
    summary = summarize(
        [
            DayEconomics(day=DAY, regime="LOW_VOL", status=PricingStatus.UNPRICEABLE),
            DayEconomics(
                day=DAY + timedelta(days=1),
                regime="HIGH_VOL",
                status=PricingStatus.UNPRICEABLE,
            ),
        ]
    )
    assert summary.n_priced == 0
    assert summary.n_unpriceable == 2
    assert summary.total_pnl == 0.0
    assert summary.mean_pnl is None
    assert summary.ev == 0.0
    assert summary.cvar_95 is None
    assert summary.max_daily_loss is None
    assert summary.win_rate is None
    assert summary.regimes["LOW_VOL"].mean_pnl is None
    empty = summarize([])
    assert empty.n_priced == 0 and empty.ev == 0.0 and empty.regimes == {}


def test_summary_to_dict_is_json_serializable() -> None:
    payload = summarize(_summary_series()).to_dict()
    encoded = json.dumps(payload, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["n_priced"] == 20
    assert math.isclose(decoded["cvar_95"], -4.0)
    assert isinstance(summary_regimes := decoded["regimes"], dict)
    assert summary_regimes["HIGH_VOL"]["n_unpriceable"] == 1


# --- JSONL loaders -------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _distances_record(day: date, regime: str = "LOW_VOL") -> dict[str, object]:
    return {
        "day": day.isoformat(),
        "down_distance": 40.0,
        "up_distance": 8.0,
        "regime": regime,
        "model_version": "tail-model-test",
        "quantile": 0.95,
    }


def _label_record(day: date, close: float = 6420.0) -> dict[str, object]:
    return {
        "day": day.isoformat(),
        "entry_et": "09:45:00",
        "close_et": "16:00:00",
        "entry_price": 6441.0,
        "close_price": close,
        "code_version": "marketpilot-0.1.0:excursion-batch-v1",
    }


def test_load_tail_distances_is_strictly_shaped(tmp_path: Path) -> None:
    path = tmp_path / "distances.jsonl"
    _write_jsonl(path, [_distances_record(DAY)])
    (loaded,) = load_tail_distances(path)
    assert loaded == _distances()

    extra = tmp_path / "extra.jsonl"
    _write_jsonl(extra, [{**_distances_record(DAY), "surprise": 1}])
    with pytest.raises(CondorEconomicsError, match="corrupt distances record"):
        load_tail_distances(extra)

    missing = tmp_path / "missing.jsonl"
    record = _distances_record(DAY)
    del record["quantile"]
    _write_jsonl(missing, [record])
    with pytest.raises(CondorEconomicsError, match="corrupt distances record"):
        load_tail_distances(missing)


def test_load_excursion_labels_ignores_provenance_extras(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    _write_jsonl(path, [_label_record(DAY)])
    (label,) = load_excursion_labels(path)
    assert label.day == DAY
    assert label.entry_price == pytest.approx(6441.0)
    assert label.close_price == pytest.approx(6420.0)

    bad = tmp_path / "bad.jsonl"
    _write_jsonl(bad, [{"day": DAY.isoformat(), "entry_price": 6441.0}])
    with pytest.raises(CondorEconomicsError, match="corrupt label record"):
        load_excursion_labels(bad)


# --- batch runner + CLI ---------------------------------------------------

BATCH_START = date(2026, 8, 17)
BATCH_PRICED = date(2026, 8, 17)
BATCH_NO_CHAIN = date(2026, 8, 18)
BATCH_NO_LABEL = date(2026, 8, 19)
BATCH_NO_DISTANCES = date(2026, 8, 20)
BATCH_END = date(2026, 8, 20)


def _batch_inputs(tmp_path: Path) -> tuple[Path, Path]:
    labels = tmp_path / "labels.jsonl"
    _write_jsonl(
        labels,
        [
            _label_record(BATCH_PRICED),
            _label_record(BATCH_NO_CHAIN),
            _label_record(BATCH_NO_DISTANCES),
        ],
    )
    distances = tmp_path / "distances.jsonl"
    _write_jsonl(
        distances,
        [
            _distances_record(BATCH_PRICED),
            _distances_record(BATCH_NO_CHAIN, regime="HIGH_VOL"),
            _distances_record(BATCH_NO_LABEL),
        ],
    )
    return labels, distances


def _batch_loader(day: date) -> ChainDay:
    if day == BATCH_NO_CHAIN:
        raise NormalizeError(f"missing landed batch for {day}")
    return _chain(_standard_quotes(), day=day)


def test_run_economics_batch_counts_every_disposition(tmp_path: Path) -> None:
    labels, distances = _batch_inputs(tmp_path)
    report = run_economics_batch(
        labels_path=labels,
        distances_path=distances,
        data_root=tmp_path / "unused-raw",
        pit_ledger_path=tmp_path / "unused-pit.jsonl",
        start=BATCH_START,
        end=BATCH_END,
        chain_loader=_batch_loader,
    )
    assert report.n_missing_chain == 1
    assert report.n_missing_labels == 1
    assert report.n_missing_distances == 1
    assert report.summary.n_priced == 1
    assert report.summary.n_unpriceable == 0
    # The one priced day closed inside the corridor: full credit kept.
    assert report.summary.total_pnl == pytest.approx(EXPECTED_CREDIT)
    assert report.summary.mean_pnl == pytest.approx(EXPECTED_CREDIT)


def test_run_economics_batch_rejects_inverted_window(tmp_path: Path) -> None:
    labels, distances = _batch_inputs(tmp_path)
    with pytest.raises(CondorEconomicsError, match="must not be after end"):
        run_economics_batch(
            labels_path=labels,
            distances_path=distances,
            data_root=tmp_path,
            pit_ledger_path=tmp_path / "pit.jsonl",
            start=BATCH_END,
            end=BATCH_START,
            chain_loader=_batch_loader,
        )


def test_cli_evaluate_economics_prints_summary_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    labels, distances = _batch_inputs(tmp_path)
    monkeypatch.setattr(
        "marketpilot.validation.condor_economics.normalize_day",
        lambda *, data_root, pit_ledger_path, day: _batch_loader(day),
    )
    code = main(
        [
            "evaluate-economics",
            "--start",
            BATCH_START.isoformat(),
            "--end",
            BATCH_END.isoformat(),
            "--labels",
            str(labels),
            "--distances",
            str(distances),
            "--data-root",
            str(tmp_path / "unused-raw"),
            "--pit-ledger",
            str(tmp_path / "unused-pit.jsonl"),
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "OK"
    assert out["n_priced"] == 1
    assert out["n_missing_chain"] == 1
    assert out["n_missing_labels"] == 1
    assert out["n_missing_distances"] == 1
    assert math.isclose(out["total_pnl"], EXPECTED_CREDIT)
    assert out["regimes"]["LOW_VOL"]["n_priced"] == 1


def test_cli_evaluate_economics_fails_loudly_on_bad_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "evaluate-economics",
            "--start",
            BATCH_START.isoformat(),
            "--end",
            BATCH_END.isoformat(),
            "--labels",
            str(tmp_path / "absent-labels.jsonl"),
            "--distances",
            str(tmp_path / "absent-distances.jsonl"),
        ]
    )
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "FAIL"


# --- fee-aware economics ----------------------------------------------------

# Custom schedule: 4 legs x 1 contract x (1.00 + 0.25) + 0 = 5.00 USD,
# i.e. a 0.05-point drag at the 100 USD/point SPXW multiplier.
FEE_BODY = """version = "fees-v1"

[per_contract]
commission_usd = 1.0
regulatory_usd = 0.25

[per_order]
fixed_usd = 0.0
"""
FEE_POINTS = 5.00 / 100.0


def _fee_schedule_path(tmp_path: Path) -> Path:
    path = tmp_path / "fees.toml"
    path.write_text(FEE_BODY, encoding="utf-8")
    return path


def test_day_economics_zero_fees_net_equals_gross() -> None:
    day = _priced_day(DAY, 1.8)
    assert day.fees == 0.0
    assert day.net_pnl == pytest.approx(day.pnl)


def test_day_economics_fees_derive_and_validate_net() -> None:
    day = DayEconomics(
        day=DAY,
        regime="LOW_VOL",
        status=PricingStatus.PRICED,
        credit=1.8,
        max_loss=3.2,
        settlement_loss=0.0,
        pnl=1.8,
        put_breached=False,
        call_breached=False,
        fees=FEE_POINTS,
    )
    assert day.net_pnl == pytest.approx(1.8 - FEE_POINTS)
    explicit = DayEconomics(
        day=DAY,
        regime="LOW_VOL",
        status=PricingStatus.PRICED,
        credit=1.8,
        max_loss=3.2,
        settlement_loss=0.0,
        pnl=1.8,
        put_breached=False,
        call_breached=False,
        fees=FEE_POINTS,
        net_pnl=1.8 - FEE_POINTS,
    )
    assert explicit.net_pnl == pytest.approx(1.75)
    with pytest.raises(CondorEconomicsError, match="net_pnl must equal pnl minus fees"):
        DayEconomics(
            day=DAY,
            regime="LOW_VOL",
            status=PricingStatus.PRICED,
            credit=1.8,
            max_loss=3.2,
            settlement_loss=0.0,
            pnl=1.8,
            put_breached=False,
            call_breached=False,
            fees=FEE_POINTS,
            net_pnl=1.8,
        )
    with pytest.raises(CondorEconomicsError, match="fees must not be negative"):
        DayEconomics(
            day=DAY,
            regime="LOW_VOL",
            status=PricingStatus.PRICED,
            credit=1.8,
            max_loss=3.2,
            settlement_loss=0.0,
            pnl=1.8,
            put_breached=False,
            call_breached=False,
            fees=-0.01,
        )
    with pytest.raises(CondorEconomicsError, match="UNPRICEABLE day must not carry fees"):
        DayEconomics(
            day=DAY,
            regime="LOW_VOL",
            status=PricingStatus.UNPRICEABLE,
            fees=FEE_POINTS,
        )


def test_evaluate_day_applies_fee_drag_to_net_pnl() -> None:
    result = evaluate_day(
        chain=_chain(_standard_quotes()),
        entry=ENTRY,
        center=6441.0,
        distances=_distances(),
        close_price=6420.0,
        fees=FEE_POINTS,
    )
    assert result.status is PricingStatus.PRICED
    assert result.pnl == pytest.approx(EXPECTED_CREDIT)
    assert result.fees == pytest.approx(FEE_POINTS)
    assert result.net_pnl == pytest.approx(EXPECTED_CREDIT - FEE_POINTS)
    # Default remains the fee-free v1 behavior.
    fee_free = evaluate_day(
        chain=_chain(_standard_quotes()),
        entry=ENTRY,
        center=6441.0,
        distances=_distances(),
        close_price=6420.0,
    )
    assert fee_free.fees == 0.0
    assert fee_free.net_pnl == pytest.approx(EXPECTED_CREDIT)
    with pytest.raises(CondorEconomicsError, match="fees must not be negative"):
        evaluate_day(
            chain=_chain(_standard_quotes()),
            entry=ENTRY,
            center=6441.0,
            distances=_distances(),
            close_price=6420.0,
            fees=-0.01,
        )


def test_summarize_fee_aggregates() -> None:
    days = [
        DayEconomics(
            day=date(2026, 8, 3),
            regime="LOW_VOL",
            status=PricingStatus.PRICED,
            credit=1.8,
            max_loss=3.2,
            settlement_loss=0.0,
            pnl=1.8,
            put_breached=False,
            call_breached=False,
            fees=FEE_POINTS,
        ),
        DayEconomics(
            day=date(2026, 8, 4),
            regime="LOW_VOL",
            status=PricingStatus.PRICED,
            credit=1.8,
            max_loss=3.2,
            settlement_loss=3.0,
            pnl=-1.2,
            put_breached=False,
            call_breached=True,
            fees=FEE_POINTS,
        ),
        DayEconomics(
            day=date(2026, 8, 5),
            regime="LOW_VOL",
            status=PricingStatus.UNPRICEABLE,
        ),
    ]
    summary = summarize(days)
    assert summary.total_pnl == pytest.approx(0.6)
    assert summary.total_fees == pytest.approx(2 * FEE_POINTS)
    assert summary.net_total_pnl == pytest.approx(0.6 - 2 * FEE_POINTS)
    # Net EV is per candidate day, including the unpriceable no-trade day.
    assert summary.net_ev == pytest.approx((0.6 - 2 * FEE_POINTS) / 3)
    assert summary.ev == pytest.approx(0.6 / 3)
    payload = summary.to_dict()
    assert payload["total_fees"] == pytest.approx(2 * FEE_POINTS)
    assert payload["net_total_pnl"] == pytest.approx(0.6 - 2 * FEE_POINTS)
    assert payload["net_ev"] == pytest.approx((0.6 - 2 * FEE_POINTS) / 3)


def test_summarize_fee_free_net_equals_gross() -> None:
    summary = summarize(_summary_series())
    assert summary.total_fees == 0.0
    assert summary.net_total_pnl == pytest.approx(summary.total_pnl)
    assert summary.net_ev == pytest.approx(summary.ev)


def test_run_economics_batch_with_fees_path(tmp_path: Path) -> None:
    labels, distances = _batch_inputs(tmp_path)
    report = run_economics_batch(
        labels_path=labels,
        distances_path=distances,
        data_root=tmp_path / "unused-raw",
        pit_ledger_path=tmp_path / "unused-pit.jsonl",
        start=BATCH_START,
        end=BATCH_END,
        chain_loader=_batch_loader,
        fees_path=_fee_schedule_path(tmp_path),
    )
    assert report.summary.n_priced == 1
    assert report.summary.total_pnl == pytest.approx(EXPECTED_CREDIT)
    assert report.summary.total_fees == pytest.approx(FEE_POINTS)
    assert report.summary.net_total_pnl == pytest.approx(EXPECTED_CREDIT - FEE_POINTS)
    # One candidate day was priced; missing days are not candidates.
    assert report.summary.net_ev == pytest.approx(EXPECTED_CREDIT - FEE_POINTS)


def test_run_economics_batch_without_fees_path_is_fee_free(tmp_path: Path) -> None:
    labels, distances = _batch_inputs(tmp_path)
    report = run_economics_batch(
        labels_path=labels,
        distances_path=distances,
        data_root=tmp_path / "unused-raw",
        pit_ledger_path=tmp_path / "unused-pit.jsonl",
        start=BATCH_START,
        end=BATCH_END,
        chain_loader=_batch_loader,
    )
    assert report.summary.total_fees == 0.0
    assert report.summary.net_total_pnl == pytest.approx(report.summary.total_pnl)
    assert report.summary.net_ev == pytest.approx(report.summary.ev)


def test_run_economics_batch_rejects_bad_fee_schedule(tmp_path: Path) -> None:
    labels, distances = _batch_inputs(tmp_path)
    bad = tmp_path / "bad-fees.toml"
    bad.write_text(FEE_BODY.replace("1.0", "-1.0", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="must not be negative"):
        run_economics_batch(
            labels_path=labels,
            distances_path=distances,
            data_root=tmp_path / "unused-raw",
            pit_ledger_path=tmp_path / "unused-pit.jsonl",
            start=BATCH_START,
            end=BATCH_END,
            chain_loader=_batch_loader,
            fees_path=bad,
        )


def _run_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
) -> tuple[int, dict[str, object]]:
    labels, distances = _batch_inputs(tmp_path)
    monkeypatch.setattr(
        "marketpilot.validation.condor_economics.normalize_day",
        lambda *, data_root, pit_ledger_path, day: _batch_loader(day),
    )
    code = main(
        [
            "evaluate-economics",
            "--start",
            BATCH_START.isoformat(),
            "--end",
            BATCH_END.isoformat(),
            "--labels",
            str(labels),
            "--distances",
            str(distances),
            "--data-root",
            str(tmp_path / "unused-raw"),
            "--pit-ledger",
            str(tmp_path / "unused-pit.jsonl"),
            *extra,
        ]
    )
    return code, json.loads(capsys.readouterr().out)


def test_cli_evaluate_economics_default_fees_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The CLI default config/fees-v1.toml: 4 legs x (0.65 + 0.05) = 2.80 USD,
    # a 0.028-point drag at 100 USD/point.
    code, out = _run_cli(tmp_path, monkeypatch, capsys, [])
    assert code == 0
    assert out["status"] == "OK"
    assert math.isclose(out["total_fees"], 0.028)
    assert math.isclose(out["total_pnl"], EXPECTED_CREDIT)
    assert math.isclose(out["net_total_pnl"], EXPECTED_CREDIT - 0.028)
    assert math.isclose(out["net_ev"], EXPECTED_CREDIT - 0.028)


def test_cli_evaluate_economics_explicit_fees_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fees = _fee_schedule_path(tmp_path)
    code, out = _run_cli(tmp_path, monkeypatch, capsys, ["--fees", str(fees)])
    assert code == 0
    assert math.isclose(out["total_fees"], FEE_POINTS)
    assert math.isclose(out["net_total_pnl"], EXPECTED_CREDIT - FEE_POINTS)


def test_cli_evaluate_economics_no_fees_flag_and_empty_fees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for extra in (["--no-fees"], ["--fees", ""]):
        code, out = _run_cli(tmp_path, monkeypatch, capsys, extra)
        assert code == 0
        assert out["total_fees"] == 0.0
        assert math.isclose(out["net_total_pnl"], out["total_pnl"])
        assert math.isclose(out["net_ev"], out["ev"])
