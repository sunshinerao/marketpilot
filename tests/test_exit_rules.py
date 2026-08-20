"""Hermetic tests for validation/exit_rules.py — synthetic chains only.

No data/raw, no network, no real label or distances files: every chain is a
hand-built :class:`ChainDay` with scripted minute NBBO quotes, and batch
tests write their own JSONL inputs into ``tmp_path``.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from marketpilot.cli import main
from marketpilot.features.day_structure import ChainDay, MinuteBar, OptionQuote
from marketpilot.ingest.normalize import NormalizeError
from marketpilot.models.strikepilot.strikes import IronCondorStrikes
from marketpilot.validation.condor_economics import (
    CondorFill,
    evaluate_day,
    price_condor,
    settle_condor,
)
from marketpilot.validation.exit_rules import (
    ExitOutcome,
    ExitReason,
    ExitRule,
    ExitRuleKind,
    ExitRulesError,
    run_exit_comparison,
    simulate_exit,
)
from marketpilot.validation.tail_distances import TailDistances

DAY = date(2026, 8, 17)  # Monday; August is EDT: 09:45 ET == 13:45 UTC
ENTRY = datetime(2026, 8, 17, 13, 45, tzinfo=UTC)
QUOTE_TS = datetime(2026, 8, 17, 13, 44, tzinfo=UTC)
CLOSE_TS = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)  # 16:00 ET session close
TIME_EXIT_TS = datetime(2026, 8, 17, 19, 30, tzinfo=UTC)  # default 15:30 ET

STRIKES = IronCondorStrikes(
    short_put=6400,
    long_put=6395,
    short_call=6450,
    long_call=6455,
    put_distance=41.0,
    call_distance=9.0,
)

# Same known-fill quotes as the condor-economics tests: shorts sell at the
# bid, longs buy at the ask.
ENTRY_QUOTES = {
    (6400, "P"): (1.50, 1.70),  # short put
    (6395, "P"): (0.50, 0.60),  # long put
    (6450, "C"): (1.40, 1.60),  # short call
    (6455, "C"): (0.40, 0.50),  # long call
}
CREDIT = 1.50 + 1.40 - 0.60 - 0.50  # 1.80
# Conservative close cost carried forward from the entry quotes: shorts
# bought back at the ask, longs sold at the bid.
ENTRY_CLOSE_COST = 1.70 + 1.60 - 0.50 - 0.40  # 2.40


def _at(hour: int, minute: int, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _symbol(strike: int, right: str, day: date = DAY) -> str:
    return f"SPXW  {day:%y%m%d}{right}{strike * 1000:08d}"


def _quote(
    strike: int,
    right: str,
    bid: float | None,
    ask: float | None,
    *,
    ts: datetime,
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


def _leg_quotes(
    legs: dict[tuple[int, str], tuple[float | None, float | None]],
    *,
    ts: datetime,
    day: date = DAY,
) -> list[OptionQuote]:
    return [
        _quote(strike, right, bid, ask, ts=ts, day=day)
        for (strike, right), (bid, ask) in legs.items()
    ]


def _bar(ts: datetime = QUOTE_TS) -> MinuteBar:
    return MinuteBar(ts=ts, open=6440.0, high=6442.0, low=6438.0, close=6441.0, volume=100.0)


def _chain(quotes: list[OptionQuote], *, day: date = DAY) -> ChainDay:
    ordered = sorted(quotes, key=lambda quote: (quote.ts, quote.symbol))
    return ChainDay(day=day, underlying_bars=(_bar(),), quotes=tuple(ordered))


def _entry_quotes(*, ts: datetime = QUOTE_TS, day: date = DAY) -> list[OptionQuote]:
    return _leg_quotes(ENTRY_QUOTES, ts=ts, day=day)


def _fill(chain: ChainDay) -> CondorFill:
    fill = price_condor(chain=chain, entry=ENTRY, strikes=STRIKES)
    assert fill is not None
    assert fill.credit == pytest.approx(CREDIT)
    return fill


def _distances(day: date = DAY) -> TailDistances:
    # center 6441, down 40 -> short_put 6400; up 8 -> short_call 6450.
    return TailDistances(
        day=day,
        down_distance=40.0,
        up_distance=8.0,
        regime="LOW_VOL",
        model_version="tail-model-test",
        quantile=0.95,
    )


def _simulate(chain: ChainDay, rule: ExitRule, close_price: float = 6420.0) -> ExitOutcome:
    return simulate_exit(
        chain=chain,
        entry=ENTRY,
        fill=_fill(chain),
        strikes=STRIKES,
        close_price=close_price,
        rule=rule,
    )


# --- ExitRule / ExitOutcome contracts --------------------------------------


def test_exit_rule_defaults_and_coercion() -> None:
    rule = ExitRule(kind=ExitRuleKind.PROFIT_TARGET)
    assert rule.profit_fraction == 0.5
    assert rule.stop_multiple == 2.0
    assert rule.time_exit_et == time(15, 30)
    # String values coerce to the enum (StrEnum value form).
    assert ExitRule(kind="HOLD").kind is ExitRuleKind.HOLD  # type: ignore[arg-type]


def test_exit_rule_rejects_bad_parameters() -> None:
    with pytest.raises(ExitRulesError, match="profit_fraction"):
        ExitRule(kind=ExitRuleKind.PROFIT_TARGET, profit_fraction=0.0)
    with pytest.raises(ExitRulesError, match="profit_fraction"):
        ExitRule(kind=ExitRuleKind.PROFIT_TARGET, profit_fraction=1.5)
    with pytest.raises(ExitRulesError, match="profit_fraction"):
        ExitRule(kind=ExitRuleKind.PROFIT_TARGET, profit_fraction=math.nan)
    with pytest.raises(ExitRulesError, match="stop_multiple"):
        ExitRule(kind=ExitRuleKind.STOP_LOSS, stop_multiple=0.0)
    with pytest.raises(ExitRulesError, match="stop_multiple"):
        ExitRule(kind=ExitRuleKind.STOP_LOSS, stop_multiple=-1.0)
    with pytest.raises(ExitRulesError, match="time_exit_et"):
        ExitRule(kind=ExitRuleKind.TIME_EXIT, time_exit_et="15:30")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a valid ExitRuleKind"):
        ExitRule(kind="MOON")  # type: ignore[arg-type]


def test_exit_outcome_validates_fields() -> None:
    outcome = ExitOutcome(
        exit_ts=CLOSE_TS,
        reason="EXPIRY",  # type: ignore[arg-type]
        exit_cost=0.0,
        pnl=CREDIT,
        holding_minutes=375.0,
    )
    assert outcome.reason is ExitReason.EXPIRY
    assert outcome.exit_ts == CLOSE_TS
    with pytest.raises(ExitRulesError, match="timezone-aware"):
        ExitOutcome(
            exit_ts=datetime(2026, 8, 17, 20, 0),
            reason=ExitReason.EXPIRY,
            exit_cost=0.0,
            pnl=CREDIT,
            holding_minutes=375.0,
        )
    with pytest.raises(ExitRulesError, match="holding_minutes"):
        ExitOutcome(
            exit_ts=CLOSE_TS,
            reason=ExitReason.EXPIRY,
            exit_cost=0.0,
            pnl=CREDIT,
            holding_minutes=-1.0,
        )


def test_simulate_exit_rejects_bad_entry() -> None:
    chain = _chain(_entry_quotes())
    fill = _fill(chain)
    rule = ExitRule(kind=ExitRuleKind.HOLD)
    with pytest.raises(ExitRulesError, match="timezone-aware"):
        simulate_exit(
            chain=chain,
            entry=datetime(2026, 8, 17, 13, 45),
            fill=fill,
            strikes=STRIKES,
            close_price=6420.0,
            rule=rule,
        )
    with pytest.raises(ExitRulesError, match="chain day"):
        simulate_exit(
            chain=chain,
            entry=ENTRY + timedelta(days=1),
            fill=fill,
            strikes=STRIKES,
            close_price=6420.0,
            rule=rule,
        )
    with pytest.raises(ExitRulesError, match="session close"):
        simulate_exit(
            chain=chain,
            entry=CLOSE_TS,
            fill=fill,
            strikes=STRIKES,
            close_price=6420.0,
            rule=rule,
        )


# --- per-rule trigger semantics ---------------------------------------------


def test_profit_target_triggers_at_the_first_minute_at_or_below_threshold() -> None:
    quotes = _entry_quotes() + [
        # 13:46: cost 1.20 > 0.90 target (credit * (1 - 0.5)) — no trigger.
        *_leg_quotes(
            {
                (6400, "P"): (0.95, 1.00),
                (6450, "C"): (0.85, 0.90),
                (6395, "P"): (0.40, 0.45),
                (6455, "C"): (0.30, 0.35),
            },
            ts=_at(13, 46),
        ),
        # 13:47: cost 1.15 - 0.25 = 0.90 == threshold — triggers here.
        *_leg_quotes(
            {
                (6400, "P"): (0.55, 0.60),
                (6450, "C"): (0.50, 0.55),
                (6395, "P"): (0.15, 0.20),
                (6455, "C"): (0.10, 0.15),
            },
            ts=_at(13, 47),
        ),
    ]
    outcome = _simulate(_chain(quotes), ExitRule(kind=ExitRuleKind.PROFIT_TARGET))
    assert outcome.reason is ExitReason.PROFIT_TARGET
    assert outcome.exit_ts == _at(13, 47)
    assert outcome.exit_cost == pytest.approx(0.90)
    assert outcome.pnl == pytest.approx(CREDIT - 0.90)
    assert outcome.holding_minutes == pytest.approx(2.0)


def test_stop_loss_triggers_at_the_first_minute_at_or_above_threshold() -> None:
    quotes = _entry_quotes() + [
        # 13:46: cost 4.90 - 0.90 = 4.00 < 5.40 stop (credit * (1 + 2)) — no trigger.
        *_leg_quotes(
            {
                (6400, "P"): (2.40, 2.50),
                (6450, "C"): (2.30, 2.40),
                (6395, "P"): (0.50, 0.60),
                (6455, "C"): (0.40, 0.50),
            },
            ts=_at(13, 46),
        ),
        # 13:47: cost 6.30 - 0.70 = 5.60 >= 5.40 — triggers here.
        *_leg_quotes(
            {
                (6400, "P"): (3.10, 3.20),
                (6450, "C"): (3.00, 3.10),
                (6395, "P"): (0.40, 0.50),
                (6455, "C"): (0.30, 0.40),
            },
            ts=_at(13, 47),
        ),
    ]
    outcome = _simulate(_chain(quotes), ExitRule(kind=ExitRuleKind.STOP_LOSS))
    assert outcome.reason is ExitReason.STOP_LOSS
    assert outcome.exit_ts == _at(13, 47)
    assert outcome.exit_cost == pytest.approx(5.60)
    assert outcome.pnl == pytest.approx(CREDIT - 5.60)
    assert outcome.holding_minutes == pytest.approx(2.0)


def test_trigger_at_the_close_minute_beats_expiry() -> None:
    # No quotes between entry and the close minute: the carried-forward entry
    # quotes cost 2.40 (no trigger); at 16:00 ET exactly the cost drops to
    # 0.95 - 0.15 = 0.80 <= 0.90 — the trigger wins over EXPIRY.
    quotes = _entry_quotes() + _leg_quotes(
        {
            (6400, "P"): (0.45, 0.50),
            (6450, "C"): (0.40, 0.45),
            (6395, "P"): (0.10, 0.15),
            (6455, "C"): (0.05, 0.10),
        },
        ts=CLOSE_TS,
    )
    outcome = _simulate(_chain(quotes), ExitRule(kind=ExitRuleKind.PROFIT_TARGET))
    assert outcome.reason is ExitReason.PROFIT_TARGET
    assert outcome.exit_ts == CLOSE_TS
    assert outcome.exit_cost == pytest.approx(0.80)
    assert outcome.pnl == pytest.approx(CREDIT - 0.80)
    assert outcome.holding_minutes == pytest.approx(375.0)


def test_time_exit_closes_at_first_minute_at_or_after_the_wall_clock() -> None:
    # Only entry quotes exist; they carry forward, so every minute is
    # evaluable and the exit happens exactly at 15:30 ET (19:30 UTC).
    outcome = _simulate(_chain(_entry_quotes()), ExitRule(kind=ExitRuleKind.TIME_EXIT))
    assert outcome.reason is ExitReason.TIME_EXIT
    assert outcome.exit_ts == TIME_EXIT_TS
    assert outcome.exit_cost == pytest.approx(ENTRY_CLOSE_COST)
    assert outcome.pnl == pytest.approx(CREDIT - ENTRY_CLOSE_COST)
    assert outcome.holding_minutes == pytest.approx(345.0)


def test_time_exit_falls_back_to_expiry_when_no_late_usable_quotes() -> None:
    # From 14:00 UTC on, no leg has a usable closing side (shorts lack an
    # ask, longs lack a bid), so no minute at-or-after 15:30 ET is evaluable
    # and the position settles at expiry — never a fabricated close.
    quotes = _entry_quotes() + _leg_quotes(
        {
            (6400, "P"): (0.50, None),
            (6450, "C"): (0.40, None),
            (6395, "P"): (None, 0.20),
            (6455, "C"): (None, 0.10),
        },
        ts=_at(14, 0),
    )
    outcome = _simulate(_chain(quotes), ExitRule(kind=ExitRuleKind.TIME_EXIT))
    assert outcome.reason is ExitReason.EXPIRY
    assert outcome.exit_ts == CLOSE_TS
    assert outcome.exit_cost == pytest.approx(settle_condor(STRIKES, 6420.0))
    assert outcome.exit_cost == pytest.approx(0.0)  # close inside the corridor
    assert outcome.pnl == pytest.approx(CREDIT)
    assert outcome.holding_minutes == pytest.approx(375.0)


def test_minutes_with_an_unusable_leg_are_skipped_never_fabricated() -> None:
    quotes = _entry_quotes() + [
        # 13:46: the short asks alone would trigger (combined 0.20 <= 0.90),
        # but both long legs lack a bid — the minute is not evaluable.
        *_leg_quotes(
            {
                (6400, "P"): (0.05, 0.10),
                (6450, "C"): (0.05, 0.10),
                (6395, "P"): (None, 0.30),
                (6455, "C"): (None, 0.20),
            },
            ts=_at(13, 46),
        ),
        # 13:47: all four legs usable again; cost 0.95 - 0.15 = 0.80 triggers.
        *_leg_quotes(
            {
                (6400, "P"): (0.45, 0.50),
                (6450, "C"): (0.40, 0.45),
                (6395, "P"): (0.10, 0.15),
                (6455, "C"): (0.05, 0.10),
            },
            ts=_at(13, 47),
        ),
    ]
    outcome = _simulate(_chain(quotes), ExitRule(kind=ExitRuleKind.PROFIT_TARGET))
    # The trigger fires at 13:47 — the 13:46 minute was skipped, not priced.
    assert outcome.reason is ExitReason.PROFIT_TARGET
    assert outcome.exit_ts == _at(13, 47)
    assert outcome.exit_cost == pytest.approx(0.80)

    # Without the 13:47 recovery the rule never triggers: expiry, no
    # fabricated exit at the partially-quoted minute.
    no_recovery = _chain(quotes[: len(_entry_quotes()) + 4])
    settled = _simulate(no_recovery, ExitRule(kind=ExitRuleKind.PROFIT_TARGET))
    assert settled.reason is ExitReason.EXPIRY
    assert settled.exit_ts == CLOSE_TS


def test_hold_to_close_matches_settle_condor_and_evaluate_day() -> None:
    chain = _chain(_entry_quotes())
    close = 6380.0  # through the put wing: settlement loss is the wing width
    outcome = _simulate(chain, ExitRule(kind=ExitRuleKind.HOLD), close_price=close)
    loss = settle_condor(STRIKES, close)
    assert loss == pytest.approx(5.0)
    assert outcome.reason is ExitReason.EXPIRY
    assert outcome.exit_ts == CLOSE_TS
    assert outcome.exit_cost == pytest.approx(loss)
    assert outcome.pnl == pytest.approx(CREDIT - loss)
    assert outcome.holding_minutes == pytest.approx(375.0)
    # HOLD is exactly the v1 baseline economics for the same day.
    day = evaluate_day(
        chain=chain,
        entry=ENTRY,
        center=6441.0,
        distances=_distances(),
        close_price=close,
    )
    assert day.pnl is not None
    assert outcome.pnl == pytest.approx(day.pnl)


# --- batch comparison -------------------------------------------------------

BATCH_PRICED = DAY
BATCH_UNPRICEABLE = date(2026, 8, 18)
BATCH_NO_CHAIN = date(2026, 8, 19)
BATCH_NO_DISTANCES = date(2026, 8, 20)

# 13:50 quotes on the priced day: cost 0.95 - 0.15 = 0.80 — under the 0.90
# profit-target threshold and far from the 5.40 stop.
PROFIT_DAY_QUOTES: dict[tuple[int, str], tuple[float | None, float | None]] = {
    (6400, "P"): (0.45, 0.50),
    (6450, "C"): (0.40, 0.45),
    (6395, "P"): (0.10, 0.15),
    (6455, "C"): (0.05, 0.10),
}
PROFIT_DAY_COST = 0.80

ALL_RULES = (
    ExitRule(kind=ExitRuleKind.HOLD),
    ExitRule(kind=ExitRuleKind.PROFIT_TARGET),
    ExitRule(kind=ExitRuleKind.STOP_LOSS),
    ExitRule(kind=ExitRuleKind.TIME_EXIT),
)


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _label_record(day: date, close: float = 6420.0) -> dict[str, object]:
    return {
        "day": day.isoformat(),
        "entry_et": "09:45",
        "entry_price": 6441.0,
        "close_price": close,
        "code_version": "marketpilot-0.1.0:exit-rules-test",
    }


def _distances_record(day: date) -> dict[str, object]:
    return {
        "day": day.isoformat(),
        "down_distance": 40.0,
        "up_distance": 8.0,
        "regime": "LOW_VOL",
        "model_version": "tail-model-test",
        "quantile": 0.95,
    }


def _batch_inputs(tmp_path: Path) -> tuple[Path, Path]:
    labels = tmp_path / "labels.jsonl"
    _write_jsonl(
        labels,
        [
            _label_record(BATCH_PRICED),
            _label_record(BATCH_UNPRICEABLE),
            _label_record(BATCH_NO_CHAIN),
            _label_record(BATCH_NO_DISTANCES),
        ],
    )
    distances = tmp_path / "distances.jsonl"
    _write_jsonl(
        distances,
        [
            _distances_record(BATCH_PRICED),
            _distances_record(BATCH_UNPRICEABLE),
            _distances_record(BATCH_NO_CHAIN),
        ],
    )
    return labels, distances


def _batch_loader(day: date) -> ChainDay:
    if day == BATCH_NO_CHAIN:
        raise NormalizeError(f"missing landed batch for {day}")
    if day == BATCH_UNPRICEABLE:
        # The long-call leg never quotes: no defensible entry fill.
        quotes = [
            quote
            for quote in _entry_quotes(ts=_at(13, 44, day), day=day)
            if quote.symbol != _symbol(6455, "C", day)
        ]
        return _chain(quotes, day=day)
    if day == BATCH_PRICED:
        quotes = _entry_quotes() + _leg_quotes(PROFIT_DAY_QUOTES, ts=_at(13, 50))
        return _chain(quotes)
    raise AssertionError(f"unexpected day {day}")


def test_run_exit_comparison_aggregates_each_rule_on_the_same_fill(tmp_path: Path) -> None:
    labels, distances = _batch_inputs(tmp_path)
    report = run_exit_comparison(
        labels_path=labels,
        distances_path=distances,
        rules=ALL_RULES,
        start=BATCH_PRICED,
        end=BATCH_NO_DISTANCES,
        data_root=tmp_path / "unused-raw",
        pit_ledger_path=tmp_path / "unused-pit.jsonl",
        chain_loader=_batch_loader,
    )
    assert report.n_missing_chain == 1
    assert report.n_missing_labels == 0
    assert report.n_missing_distances == 1

    by_kind = {summary.rule.kind: summary for summary in report.rules}
    assert set(by_kind) == set(ExitRuleKind)
    # One priced day, one explicitly unpriceable day — for EVERY rule.
    for summary in report.rules:
        assert summary.n_priced == 1
        assert summary.n_unpriceable == 1
        assert summary.win_rate == pytest.approx(1.0)

    hold = by_kind[ExitRuleKind.HOLD]
    # Close 6420 is inside the corridor: full credit kept at expiry.
    assert hold.reason_counts == {
        "EXPIRY": 1,
        "PROFIT_TARGET": 0,
        "STOP_LOSS": 0,
        "TIME_EXIT": 0,
    }
    assert hold.total_pnl == pytest.approx(CREDIT)
    assert hold.mean_holding_minutes == pytest.approx(375.0)

    target = by_kind[ExitRuleKind.PROFIT_TARGET]
    assert target.reason_counts["PROFIT_TARGET"] == 1
    assert target.total_pnl == pytest.approx(CREDIT - PROFIT_DAY_COST)
    assert target.mean_pnl == pytest.approx(CREDIT - PROFIT_DAY_COST)
    assert target.cvar_95 == pytest.approx(CREDIT - PROFIT_DAY_COST)
    # EV divides by candidate days: the unpriceable day is a zero-PnL no-trade.
    assert target.ev == pytest.approx((CREDIT - PROFIT_DAY_COST) / 2)
    assert target.mean_holding_minutes == pytest.approx(5.0)

    stop = by_kind[ExitRuleKind.STOP_LOSS]
    # The cost never reaches credit * (1 + 2) = 5.40: settles at expiry.
    assert stop.reason_counts["EXPIRY"] == 1
    assert stop.total_pnl == pytest.approx(CREDIT)

    timed = by_kind[ExitRuleKind.TIME_EXIT]
    assert timed.reason_counts["TIME_EXIT"] == 1
    # The 13:50 quotes carry forward to the 15:30 ET exit.
    assert timed.total_pnl == pytest.approx(CREDIT - PROFIT_DAY_COST)
    assert timed.mean_holding_minutes == pytest.approx(345.0)

    payload = report.to_dict()
    assert payload["status"] == "OK"
    assert len(payload["rules"]) == 4


def test_run_exit_comparison_with_no_candidate_days_reports_null_stats(
    tmp_path: Path,
) -> None:
    labels = tmp_path / "labels.jsonl"
    _write_jsonl(labels, [])
    distances = tmp_path / "distances.jsonl"
    _write_jsonl(distances, [_distances_record(BATCH_PRICED)])
    report = run_exit_comparison(
        labels_path=labels,
        distances_path=distances,
        rules=(ExitRule(kind=ExitRuleKind.HOLD),),
        start=BATCH_PRICED,
        end=BATCH_PRICED,
        data_root=tmp_path / "unused-raw",
        pit_ledger_path=tmp_path / "unused-pit.jsonl",
        chain_loader=_batch_loader,
    )
    assert report.n_missing_labels == 1
    (summary,) = report.rules
    assert summary.n_priced == 0
    assert summary.n_unpriceable == 0
    assert summary.total_pnl == 0.0
    assert summary.mean_pnl is None
    assert summary.cvar_95 is None
    assert summary.win_rate is None
    assert summary.mean_holding_minutes is None
    assert summary.ev == 0.0
    assert summary.reason_counts == {reason.value: 0 for reason in ExitReason}


def test_run_exit_comparison_rejects_bad_inputs(tmp_path: Path) -> None:
    labels, distances = _batch_inputs(tmp_path)
    with pytest.raises(ExitRulesError, match="must not be after end"):
        run_exit_comparison(
            labels_path=labels,
            distances_path=distances,
            rules=ALL_RULES,
            start=BATCH_NO_DISTANCES,
            end=BATCH_PRICED,
            data_root=tmp_path,
            pit_ledger_path=tmp_path / "pit.jsonl",
            chain_loader=_batch_loader,
        )
    with pytest.raises(ExitRulesError, match="rules must not be empty"):
        run_exit_comparison(
            labels_path=labels,
            distances_path=distances,
            rules=(),
            start=BATCH_PRICED,
            end=BATCH_NO_DISTANCES,
            data_root=tmp_path,
            pit_ledger_path=tmp_path / "pit.jsonl",
            chain_loader=_batch_loader,
        )


# --- CLI ---------------------------------------------------------------------


def _cli_args(tmp_path: Path, labels: Path, distances: Path) -> list[str]:
    return [
        "evaluate-exits",
        "--start",
        BATCH_PRICED.isoformat(),
        "--end",
        BATCH_NO_DISTANCES.isoformat(),
        "--labels",
        str(labels),
        "--distances",
        str(distances),
        "--data-root",
        str(tmp_path / "unused-raw"),
        "--pit-ledger",
        str(tmp_path / "unused-pit.jsonl"),
    ]


def test_cli_evaluate_exits_prints_per_rule_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    labels, distances = _batch_inputs(tmp_path)
    # The CLI path uses the default loader: patch the module-level
    # normalize_day it delegates to (no data/raw, no network).
    monkeypatch.setattr(
        "marketpilot.validation.exit_rules.normalize_day",
        lambda *, data_root, pit_ledger_path, day: _batch_loader(day),
    )
    code = main(_cli_args(tmp_path, labels, distances))
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "OK"
    assert out["n_missing_chain"] == 1
    assert out["n_missing_distances"] == 1
    assert out["n_missing_labels"] == 0
    kinds = [summary["rule"]["kind"] for summary in out["rules"]]
    assert kinds == ["HOLD", "PROFIT_TARGET", "STOP_LOSS", "TIME_EXIT"]
    target = out["rules"][1]
    assert target["n_priced"] == 1
    assert target["n_unpriceable"] == 1
    assert math.isclose(target["total_pnl"], CREDIT - PROFIT_DAY_COST)
    assert target["reason_counts"] == {
        "EXPIRY": 0,
        "PROFIT_TARGET": 1,
        "STOP_LOSS": 0,
        "TIME_EXIT": 0,
    }
    hold = out["rules"][0]
    assert math.isclose(hold["total_pnl"], CREDIT)
    assert hold["reason_counts"]["EXPIRY"] == 1


def test_cli_evaluate_exits_fails_loudly_on_bad_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "evaluate-exits",
            "--start",
            BATCH_PRICED.isoformat(),
            "--end",
            BATCH_NO_DISTANCES.isoformat(),
            "--labels",
            str(tmp_path / "absent-labels.jsonl"),
            "--distances",
            str(tmp_path / "absent-distances.jsonl"),
        ]
    )
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "FAIL"
