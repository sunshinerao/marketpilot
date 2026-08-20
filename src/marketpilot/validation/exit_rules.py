"""Exit-rule economics simulation for the StrikePilot 0DTE iron condor.

The trader does not necessarily hold a 0DTE condor to expiry. This module
compares four exit rules honestly on the same real data and — within a day —
on the *same* conservative entry fill (:func:`price_condor` from
:mod:`marketpilot.validation.condor_economics`):

- **HOLD** (baseline): never exits early; the position settles at expiry with
  :func:`settle_condor` semantics — identical to the v1 economics.
- **PROFIT_TARGET**: buy the condor back at the first evaluable minute whose
  conservative close cost is at most ``credit * (1 - profit_fraction)`` —
  i.e. the buyback locks in ``profit_fraction`` of the entry credit.
- **STOP_LOSS**: buy the condor back at the first evaluable minute whose
  conservative close cost is at least ``credit * (1 + stop_multiple)`` — a
  loss of ``stop_multiple`` times the credit.
- **TIME_EXIT**: close at the first evaluable minute at-or-after a fixed ET
  wall clock (default 15:30); when no such minute exists, settle at expiry.

Conservative close cost (the debit to buy the condor back)::

    cost = (ask_short_put + ask_short_call) - (bid_long_put + bid_long_call)

Closing is the mirror of opening: the shorts are bought back at their ASK,
the longs are sold at their BID. Quote usability reuses the exact entry
semantics (:func:`_buy_price` / :func:`_sell_price`): each leg rides its
latest quote at-or-before the minute (point-in-time, never lookahead), and a
minute where any leg's latest quote is missing, crossed, or shows a zero bid
on a leg being sold is *skipped, never fabricated* — a bad quote poisons
later minutes until a good one supersedes it.

Precedence: when a trigger and EXPIRY race in the same minute (the close
minute is both evaluable and the session end), the trigger wins — it is the
earlier decision. EXPIRY outcomes always use :func:`settle_condor`
(intrinsic settlement), never the last quoted close cost.

Honesty rules honored here: one entry fill per day shared by every rule, so
the comparison isolates the exit policy; unpriceable entry days are counted
explicitly for every rule; EV divides by candidate days (an unpriceable day
is a zero-PnL no-trade), matching :func:`summarize` in condor_economics.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from marketpilot.features.day_structure import ChainDay, OptionQuote
from marketpilot.ingest.normalize import normalize_day
from marketpilot.models.strikepilot.strikes import (
    IronCondorStrikes,
    select_iron_condor_strikes,
)
from marketpilot.validation.condor_economics import (
    ET,
    ChainLoader,
    CondorFill,
    _buy_price,
    _cvar_95,
    _mean,
    _sell_price,
    _win_rate,
    load_excursion_labels,
    load_tail_distances,
    osi_symbol,
    price_condor,
    settle_condor,
)

#: SPXW 0DTE contracts settle at the regular-session close, 16:00 ET.
SESSION_CLOSE_ET = time(16, 0)

_ONE_MINUTE = timedelta(minutes=1)


class ExitRulesError(ValueError):
    """Raised when exit-rule inputs violate their contract."""


class ExitRuleKind(StrEnum):
    """The four exit policies under comparison."""

    HOLD = "HOLD"
    PROFIT_TARGET = "PROFIT_TARGET"
    STOP_LOSS = "STOP_LOSS"
    TIME_EXIT = "TIME_EXIT"


class ExitReason(StrEnum):
    """How one simulated position was closed."""

    PROFIT_TARGET = "PROFIT_TARGET"
    STOP_LOSS = "STOP_LOSS"
    TIME_EXIT = "TIME_EXIT"
    EXPIRY = "EXPIRY"


def _finite(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ExitRulesError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class ExitRule:
    """Configuration of one exit policy.

    Only the field matching ``kind`` is consulted, but every field is always
    validated so a misconfigured rule fails loudly instead of silently
    running with a meaningless parameter.
    """

    kind: ExitRuleKind
    profit_fraction: float = 0.5
    stop_multiple: float = 2.0
    time_exit_et: time = time(15, 30)

    def __post_init__(self) -> None:
        # ExitRuleKind(...) accepts the enum itself or its string value.
        object.__setattr__(self, "kind", ExitRuleKind(self.kind))
        profit_fraction = _finite(self.profit_fraction, "profit_fraction")
        if not 0.0 < profit_fraction <= 1.0:
            raise ExitRulesError("profit_fraction must be within (0, 1]")
        object.__setattr__(self, "profit_fraction", profit_fraction)
        stop_multiple = _finite(self.stop_multiple, "stop_multiple")
        if stop_multiple <= 0.0:
            raise ExitRulesError("stop_multiple must be positive")
        object.__setattr__(self, "stop_multiple", stop_multiple)
        if not isinstance(self.time_exit_et, time):
            raise ExitRulesError("time_exit_et must be a datetime.time")


@dataclass(frozen=True, slots=True)
class ExitOutcome:
    """The simulated close of one condor position under one rule.

    ``exit_cost`` is the conservative debit to buy the condor back for early
    exits, or the intrinsic settlement loss for EXPIRY; ``pnl`` is always
    ``fill.credit - exit_cost``. ``holding_minutes`` runs from the entry
    instant to ``exit_ts``.
    """

    exit_ts: datetime
    reason: ExitReason
    exit_cost: float
    pnl: float
    holding_minutes: float

    def __post_init__(self) -> None:
        if self.exit_ts.tzinfo is None or self.exit_ts.utcoffset() is None:
            raise ExitRulesError("exit_ts must be timezone-aware")
        object.__setattr__(self, "exit_ts", self.exit_ts.astimezone(UTC))
        object.__setattr__(self, "reason", ExitReason(self.reason))
        object.__setattr__(self, "exit_cost", _finite(self.exit_cost, "exit_cost"))
        object.__setattr__(self, "pnl", _finite(self.pnl, "pnl"))
        holding = _finite(self.holding_minutes, "holding_minutes")
        if holding < 0:
            raise ExitRulesError("holding_minutes must not be negative")
        object.__setattr__(self, "holding_minutes", holding)


def _close_cost(
    latest: Mapping[str, OptionQuote],
    *,
    short_put_symbol: str,
    long_put_symbol: str,
    short_call_symbol: str,
    long_call_symbol: str,
) -> float | None:
    """Conservative debit to close the condor, or None when not evaluable.

    Shorts are bought back at the ask, longs sold at the bid — the mirror of
    the entry sides, with identical usability semantics.
    """

    short_put = _buy_price(latest.get(short_put_symbol))
    short_call = _buy_price(latest.get(short_call_symbol))
    long_put = _sell_price(latest.get(long_put_symbol))
    long_call = _sell_price(latest.get(long_call_symbol))
    if short_put is None or short_call is None or long_put is None or long_call is None:
        return None
    return _finite(short_put + short_call - long_put - long_call, "exit_cost")


def simulate_exit(
    *,
    chain: ChainDay,
    entry: datetime,
    fill: CondorFill,
    strikes: IronCondorStrikes,
    close_price: float,
    rule: ExitRule,
) -> ExitOutcome:
    """Simulate one exit rule on one day's chain, minute by minute.

    Steps from ``entry`` (exclusive) to the 16:00 ET session close
    (inclusive). Each minute is priced from every leg's latest quote
    at-or-before that minute; minutes where any leg is unusable are skipped.
    HOLD never looks at intraday quotes and settles at expiry. When no early
    trigger fires — or, for TIME_EXIT, no evaluable minute exists at-or-after
    ``time_exit_et`` — the position settles at expiry with
    :func:`settle_condor` semantics.
    """

    if entry.tzinfo is None or entry.utcoffset() is None:
        raise ExitRulesError("entry must be timezone-aware")
    entry_ts = entry.astimezone(UTC)
    if entry_ts.astimezone(ET).date() != chain.day:
        raise ExitRulesError("entry must fall on the chain day")
    close_ts = datetime.combine(chain.day, SESSION_CLOSE_ET, tzinfo=ET).astimezone(UTC)
    if entry_ts >= close_ts:
        raise ExitRulesError("entry must be before the session close")

    def expiry() -> ExitOutcome:
        loss = settle_condor(strikes, close_price)
        return ExitOutcome(
            exit_ts=close_ts,
            reason=ExitReason.EXPIRY,
            exit_cost=loss,
            pnl=_finite(fill.credit - loss, "pnl"),
            holding_minutes=(close_ts - entry_ts).total_seconds() / 60.0,
        )

    if rule.kind is ExitRuleKind.HOLD:
        return expiry()

    day = chain.day
    short_put_symbol = osi_symbol(day=day, right="P", strike=strikes.short_put)
    long_put_symbol = osi_symbol(day=day, right="P", strike=strikes.long_put)
    short_call_symbol = osi_symbol(day=day, right="C", strike=strikes.short_call)
    long_call_symbol = osi_symbol(day=day, right="C", strike=strikes.long_call)
    profit_threshold = fill.credit * (1.0 - rule.profit_fraction)
    stop_threshold = fill.credit * (1.0 + rule.stop_multiple)
    time_exit_ts = datetime.combine(day, rule.time_exit_et, tzinfo=ET).astimezone(UTC)

    # Single pass: the quote pointer only moves forward, and each minute
    # reuses the running per-symbol latest-quote map (quotes are
    # time-ordered by the ChainDay contract).
    latest: dict[str, OptionQuote] = {}
    index = 0
    quotes = chain.quotes
    minute = entry_ts + _ONE_MINUTE
    while minute <= close_ts:
        while index < len(quotes) and quotes[index].ts <= minute:
            latest[quotes[index].symbol] = quotes[index]
            index += 1
        cost = _close_cost(
            latest,
            short_put_symbol=short_put_symbol,
            long_put_symbol=long_put_symbol,
            short_call_symbol=short_call_symbol,
            long_call_symbol=long_call_symbol,
        )
        if cost is not None:
            reason: ExitReason | None = None
            if rule.kind is ExitRuleKind.PROFIT_TARGET and cost <= profit_threshold:
                reason = ExitReason.PROFIT_TARGET
            elif rule.kind is ExitRuleKind.STOP_LOSS and cost >= stop_threshold:
                reason = ExitReason.STOP_LOSS
            elif rule.kind is ExitRuleKind.TIME_EXIT and minute >= time_exit_ts:
                reason = ExitReason.TIME_EXIT
            if reason is not None:
                # The trigger wins even on the close minute (it is the
                # earlier decision; EXPIRY is the fallback).
                return ExitOutcome(
                    exit_ts=minute,
                    reason=reason,
                    exit_cost=cost,
                    pnl=_finite(fill.credit - cost, "pnl"),
                    holding_minutes=(minute - entry_ts).total_seconds() / 60.0,
                )
        minute += _ONE_MINUTE
    return expiry()


@dataclass(frozen=True, slots=True)
class RuleSummary:
    """Aggregate economics of one exit rule over a batch of days.

    PnL aggregates cover days with a defensible entry fill only; unpriceable
    days are counted and included in ``ev`` (per-candidate-day expected
    value, treating an unpriceable day as a zero-PnL no-trade). With no
    priced days the per-day statistics are None — never a fabricated zero.
    ``reason_counts`` always carries every :class:`ExitReason` key.
    """

    rule: ExitRule
    n_priced: int
    n_unpriceable: int
    total_pnl: float
    mean_pnl: float | None
    ev: float
    cvar_95: float | None
    win_rate: float | None
    mean_holding_minutes: float | None
    reason_counts: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": {
                "kind": self.rule.kind.value,
                "profit_fraction": self.rule.profit_fraction,
                "stop_multiple": self.rule.stop_multiple,
                "time_exit_et": self.rule.time_exit_et.isoformat(),
            },
            "n_priced": self.n_priced,
            "n_unpriceable": self.n_unpriceable,
            "total_pnl": self.total_pnl,
            "mean_pnl": self.mean_pnl,
            "ev": self.ev,
            "cvar_95": self.cvar_95,
            "win_rate": self.win_rate,
            "mean_holding_minutes": self.mean_holding_minutes,
            "reason_counts": {
                key: self.reason_counts[key] for key in sorted(self.reason_counts)
            },
        }


def _summarize_rule(
    rule: ExitRule,
    outcomes: Sequence[ExitOutcome],
    n_unpriceable: int,
) -> RuleSummary:
    results = tuple(outcomes)
    pnl_values = tuple(outcome.pnl for outcome in results)
    total_pnl = _finite(math.fsum(pnl_values), "total_pnl") if pnl_values else 0.0
    n_candidates = len(results) + n_unpriceable
    ev = total_pnl / n_candidates if n_candidates else 0.0
    reason_counts = {reason.value: 0 for reason in ExitReason}
    for outcome in results:
        reason_counts[outcome.reason.value] += 1
    holdings = tuple(outcome.holding_minutes for outcome in results)
    return RuleSummary(
        rule=rule,
        n_priced=len(results),
        n_unpriceable=n_unpriceable,
        total_pnl=total_pnl,
        mean_pnl=_mean(pnl_values) if pnl_values else None,
        ev=_finite(ev, "ev"),
        cvar_95=_cvar_95(pnl_values) if pnl_values else None,
        win_rate=_win_rate(pnl_values) if pnl_values else None,
        mean_holding_minutes=_mean(holdings) if holdings else None,
        reason_counts=MappingProxyType(reason_counts),
    )


@dataclass(frozen=True, slots=True)
class ExitComparisonReport:
    """Aggregate result of one :func:`run_exit_comparison` window."""

    start: date
    end: date
    rules: tuple[RuleSummary, ...]
    n_missing_chain: int
    n_missing_distances: int
    n_missing_labels: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "OK",
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "n_missing_chain": self.n_missing_chain,
            "n_missing_distances": self.n_missing_distances,
            "n_missing_labels": self.n_missing_labels,
            "rules": [summary.to_dict() for summary in self.rules],
        }


def run_exit_comparison(
    *,
    labels_path: str | Path,
    distances_path: str | Path,
    rules: Sequence[ExitRule],
    start: date,
    end: date,
    data_root: str | Path,
    pit_ledger_path: str | Path,
    chain_loader: ChainLoader | None = None,
) -> ExitComparisonReport:
    """Compare exit rules over ``[start, end]`` on shared entry fills.

    For each joined day (labels center/close + :class:`TailDistances` +
    chain via ``normalize_day``) the entry fill is priced once with
    :func:`price_condor`, then *every* rule is simulated on that same fill —
    the comparison isolates the exit policy. Days with no entry fill are
    UNPRICEABLE for every rule (explicit counts). Missing labels, missing
    distances, and un-normalizable chains are counted, never silently
    dropped.
    """

    if start > end:
        raise ExitRulesError(f"start {start} must not be after end {end}")
    rule_list = tuple(rules)
    if not rule_list:
        raise ExitRulesError("rules must not be empty")

    loader: ChainLoader
    if chain_loader is None:

        def default_loader(day: date) -> ChainDay:
            return normalize_day(
                data_root=data_root,
                pit_ledger_path=pit_ledger_path,
                day=day,
            )

        loader = default_loader
    else:
        loader = chain_loader

    labels = {
        label.day: label
        for label in load_excursion_labels(labels_path)
        if start <= label.day <= end
    }
    distances = {
        record.day: record
        for record in load_tail_distances(distances_path)
        if start <= record.day <= end
    }

    n_missing_chain = 0
    n_missing_labels = 0
    outcomes: list[list[ExitOutcome]] = [[] for _ in rule_list]
    unpriceable = [0] * len(rule_list)
    for day in sorted(set(labels) | set(distances)):
        label = labels.get(day)
        record = distances.get(day)
        if label is None:
            n_missing_labels += 1
            continue
        if record is None:
            continue  # counted once below
        entry = datetime.combine(day, label.entry_et, tzinfo=ET).astimezone(UTC)
        try:
            chain = loader(day)
        except Exception:  # NormalizeError or structural failure: explicit skip
            n_missing_chain += 1
            continue
        strikes = select_iron_condor_strikes(
            center=label.entry_price,
            up_tail=record.up_distance,
            down_tail=record.down_distance,
        )
        fill = price_condor(chain=chain, entry=entry, strikes=strikes)
        if fill is None:
            for index in range(len(rule_list)):
                unpriceable[index] += 1
            continue
        for index, rule in enumerate(rule_list):
            outcomes[index].append(
                simulate_exit(
                    chain=chain,
                    entry=entry,
                    fill=fill,
                    strikes=strikes,
                    close_price=label.close_price,
                    rule=rule,
                )
            )
    n_missing_distances = sum(1 for day in labels if day not in distances)
    return ExitComparisonReport(
        start=start,
        end=end,
        rules=tuple(
            _summarize_rule(rule, outcomes[index], unpriceable[index])
            for index, rule in enumerate(rule_list)
        ),
        n_missing_chain=n_missing_chain,
        n_missing_distances=n_missing_distances,
        n_missing_labels=n_missing_labels,
    )
