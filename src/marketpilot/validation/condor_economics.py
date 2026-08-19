"""Workstream G: iron-condor economics and per-day PnL profile.

Conservative, fail-closed economics for the StrikePilot 0DTE iron condor:

- :func:`price_condor` values the opening fill at the entry minute from the
  day's normalized NBBO chain (:class:`ChainDay`). Shorts fill at the BID,
  longs at the ASK, each leg using the latest quote at-or-before the entry
  instant. Any leg without a usable quote (missing symbol, missing side,
  crossed book, zero bid on a short leg) makes the day UNPRICEABLE — no fill
  is ever fabricated.
- :func:`settle_condor` computes the intrinsic settlement loss at the expiry
  close, capped by the wing width; day PnL is ``credit - settlement_loss``.
- :func:`evaluate_day` joins strikes (workstream F :class:`TailDistances`),
  the entry fill, and the settlement into one frozen :class:`DayEconomics`.
- :func:`summarize` aggregates per-day economics into an
  :class:`EconomicsSummary` with EV, CVaR-95, and a per-regime breakdown.
- :func:`run_economics_batch` is the batch join used by the
  ``marketpilot evaluate-economics`` CLI: labels (implied center/close),
  tail distances, and chains via :func:`normalize_day`. Unpriceable and
  missing-chain days are explicit counts, never silent drops.

Honesty rules honored here: every fill uses only quotes visible at-or-before
the entry instant; prices are point-in-time NBBO, never midpoints; and an
UNPRICEABLE day contributes zero to PnL aggregates but is counted.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from marketpilot.features.day_structure import ChainDay, OptionQuote
from marketpilot.ingest.normalize import normalize_day
from marketpilot.models.strikepilot.strikes import (
    IronCondorStrikes,
    select_iron_condor_strikes,
)
from marketpilot.validation.tail_distances import TailDistances

ET = ZoneInfo("America/New_York")

#: 0DTE contracts are SPXW weeklies; strikes ride the 5-point grid with
#: 5-point wings (same constants as the strike selector's defaults).
OPTION_ROOT = "SPXW"

DEFAULT_DISTANCES_PATH = Path("data/derived/tail-distances/distances.jsonl")

_DISTANCE_KEYS = frozenset(
    {"day", "down_distance", "up_distance", "regime", "model_version", "quantile"}
)
_LABEL_REQUIRED_KEYS = frozenset({"day", "entry_price", "close_price", "entry_et"})


class CondorEconomicsError(ValueError):
    """Raised when condor-economics inputs violate their contract."""


class PricingStatus(StrEnum):
    """Whether a day produced a defensible conservative entry fill."""

    PRICED = "PRICED"
    UNPRICEABLE = "UNPRICEABLE"


def _finite(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise CondorEconomicsError(f"{name} must be finite")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CondorEconomicsError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def osi_symbol(*, day: date, right: str, strike: int) -> str:
    """The 21-character padded OSI raw symbol for one SPXW 0DTE contract.

    Layout: root left-justified to 6, expiry YYMMDD, right flag (``P``/``C``),
    strike times 1000 zero-padded to 8 — matching the databento
    ``raw_symbol`` symbology carried by :class:`OptionQuote`.
    """

    if right not in {"P", "C"}:
        raise CondorEconomicsError("right must be 'P' or 'C'")
    if strike <= 0:
        raise CondorEconomicsError("strike must be positive")
    symbol = f"{OPTION_ROOT:<6}{day:%y%m%d}{right}{strike * 1000:08d}"
    if len(symbol) != 21:  # defensive: the ChainDay contract requires exactly 21
        raise CondorEconomicsError(f"malformed OSI symbol {symbol!r}")
    return symbol


@dataclass(frozen=True, slots=True)
class CondorFill:
    """A conservative simultaneous opening fill of the four condor legs.

    Prices are per-share premium points at the defensive NBBO side: shorts
    sold at their bid, longs bought at their ask. ``credit`` is the net
    premium collected (short proceeds minus long cost) and may be zero or
    negative when the market quotes it that way — it is never floored or
    otherwise "improved".
    """

    filled_at: datetime
    strikes: IronCondorStrikes
    short_put_price: float
    long_put_price: float
    short_call_price: float
    long_call_price: float
    credit: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "filled_at", _utc(self.filled_at, "filled_at"))
        for name in (
            "short_put_price",
            "long_put_price",
            "short_call_price",
            "long_call_price",
            "credit",
        ):
            value = _finite(getattr(self, name), name)
            if name != "credit" and value < 0:
                raise CondorEconomicsError(f"{name} must not be negative")
            object.__setattr__(self, name, value)
        expected = (
            self.short_put_price
            + self.short_call_price
            - self.long_put_price
            - self.long_call_price
        )
        if not math.isclose(self.credit, expected, rel_tol=0.0, abs_tol=1e-9):
            raise CondorEconomicsError("credit must equal short proceeds minus long cost")


def _latest_quotes_at_or_before(
    quotes: tuple[OptionQuote, ...], entry: datetime
) -> dict[str, OptionQuote]:
    """Index the latest quote per symbol with ``ts <= entry`` (PIT visibility)."""

    latest: dict[str, OptionQuote] = {}
    for quote in quotes:  # ChainDay quotes are time-ordered
        if quote.ts > entry:
            break
        latest[quote.symbol] = quote
    return latest


def _sell_price(quote: OptionQuote | None) -> float | None:
    """Conservative short-leg fill price (the bid), or None when unusable."""

    if quote is None or quote.bid is None or quote.bid <= 0:
        # A short leg quoted at a zero bid has no defensible premium to collect.
        return None
    if quote.ask is not None and quote.bid > quote.ask:
        return None  # crossed book (defensive; the ChainDay contract forbids it)
    return quote.bid


def _buy_price(quote: OptionQuote | None) -> float | None:
    """Conservative long-leg fill price (the ask), or None when unusable."""

    if quote is None or quote.ask is None:
        return None
    if quote.bid is not None and quote.bid > quote.ask:
        return None  # crossed book (defensive; the ChainDay contract forbids it)
    return quote.ask


def price_condor(
    *,
    chain: ChainDay,
    entry: datetime,
    strikes: IronCondorStrikes,
) -> CondorFill | None:
    """Conservative entry fill at the entry minute, or None when unpriceable.

    Each leg uses the latest NBBO at-or-before ``entry``: the short put/call
    sell at their BID, the long put/call buy at their ASK. If any leg lacks a
    usable quote — the symbol never quoted, the needed side is absent, the
    book is crossed, or a short leg shows a zero bid — the day is
    UNPRICEABLE and no fill is fabricated.
    """

    moment = _utc(entry, "entry")
    latest = _latest_quotes_at_or_before(chain.quotes, moment)
    day = chain.day
    short_put = _sell_price(
        latest.get(osi_symbol(day=day, right="P", strike=strikes.short_put))
    )
    long_put = _buy_price(latest.get(osi_symbol(day=day, right="P", strike=strikes.long_put)))
    short_call = _sell_price(
        latest.get(osi_symbol(day=day, right="C", strike=strikes.short_call))
    )
    long_call = _buy_price(
        latest.get(osi_symbol(day=day, right="C", strike=strikes.long_call))
    )
    if short_put is None or long_put is None or short_call is None or long_call is None:
        return None
    credit = _finite(short_put + short_call - long_put - long_call, "credit")
    return CondorFill(
        filled_at=moment,
        strikes=strikes,
        short_put_price=short_put,
        long_put_price=long_put,
        short_call_price=short_call,
        long_call_price=long_call,
        credit=credit,
    )


def settle_condor(strikes: IronCondorStrikes, close_price: float) -> float:
    """Intrinsic settlement loss of the condor at the expiry close.

    Short-leg losses minus long-leg recovery, capped by the wing width::

        short_put_loss  = max(0, K_sp - close)
        short_call_loss = max(0, close - K_sc)
        recovery        = max(0, K_lp - close) + max(0, close - K_lc)
        loss            = min(short_put_loss + short_call_loss - recovery, wing)

    Day PnL is ``credit - loss`` (computed by :func:`evaluate_day`).
    """

    close = _finite(close_price, "close_price")
    if close <= 0:
        raise CondorEconomicsError("close_price must be positive")
    wing = strikes.short_put - strikes.long_put
    if wing <= 0 or strikes.long_call - strikes.short_call != wing:
        raise CondorEconomicsError("strikes must have symmetric positive wings")
    short_put_loss = max(0.0, strikes.short_put - close)
    short_call_loss = max(0.0, close - strikes.short_call)
    recovery = max(0.0, strikes.long_put - close) + max(0.0, close - strikes.long_call)
    loss = short_put_loss + short_call_loss - recovery
    # Only one side can finish in the money, so loss already lies within
    # [0, wing]; the clamp is a defensive guard on the declared bound.
    return _finite(min(max(loss, 0.0), float(wing)), "settlement_loss")


@dataclass(frozen=True, slots=True)
class DayEconomics:
    """One trading day's conservative condor economics.

    PRICED days carry the fill credit, the declared max loss
    (``wing_width - credit``), the intrinsic settlement loss, the day PnL,
    and close-beyond-short-strike breach flags. UNPRICEABLE days suppress all
    numeric fields — there is no defensible fill to report.
    """

    day: date
    regime: str
    status: PricingStatus
    credit: float | None = None
    max_loss: float | None = None
    settlement_loss: float | None = None
    pnl: float | None = None
    put_breached: bool | None = None
    call_breached: bool | None = None

    def __post_init__(self) -> None:
        if not self.regime.strip():
            raise CondorEconomicsError("regime must not be blank")
        numeric = ("credit", "max_loss", "settlement_loss", "pnl")
        if self.status is PricingStatus.PRICED:
            for name in numeric:
                value = getattr(self, name)
                if value is None:
                    raise CondorEconomicsError(f"PRICED day requires {name}")
                object.__setattr__(self, name, _finite(value, name))
            if self.put_breached is None or self.call_breached is None:
                raise CondorEconomicsError("PRICED day requires breach flags")
            if self.max_loss is not None and self.max_loss < 0:
                raise CondorEconomicsError("max_loss must not be negative")
            if self.settlement_loss is not None and self.settlement_loss < 0:
                raise CondorEconomicsError("settlement_loss must not be negative")
            if (
                self.pnl is not None
                and self.credit is not None
                and self.settlement_loss is not None
                and not math.isclose(
                    self.pnl, self.credit - self.settlement_loss, rel_tol=0.0, abs_tol=1e-9
                )
            ):
                raise CondorEconomicsError("pnl must equal credit minus settlement_loss")
        else:
            for name in numeric:
                if getattr(self, name) is not None:
                    raise CondorEconomicsError(f"UNPRICEABLE day must not carry {name}")
            if self.put_breached is not None or self.call_breached is not None:
                raise CondorEconomicsError("UNPRICEABLE day must not carry breach flags")


def evaluate_day(
    *,
    chain: ChainDay,
    entry: datetime,
    center: float,
    distances: TailDistances,
    close_price: float,
) -> DayEconomics:
    """Join strikes, the conservative entry fill, and settlement for one day.

    Strikes come from :func:`select_iron_condor_strikes` on the implied
    ``center`` and the workstream-F tail distances (5-point grid, 5-point
    wings). An unusable entry fill yields an UNPRICEABLE day; otherwise the
    day PnL is ``credit - settlement_loss`` with breach flags marking whether
    the close finished beyond either short strike.
    """

    if distances.day != chain.day:
        raise CondorEconomicsError(
            f"distances day {distances.day} does not match chain day {chain.day}"
        )
    center_value = _finite(center, "center")
    strikes = select_iron_condor_strikes(
        center=center_value,
        up_tail=distances.up_distance,
        down_tail=distances.down_distance,
    )
    fill = price_condor(chain=chain, entry=entry, strikes=strikes)
    if fill is None:
        return DayEconomics(
            day=chain.day,
            regime=distances.regime,
            status=PricingStatus.UNPRICEABLE,
        )
    settlement_loss = settle_condor(strikes, close_price)
    wing = float(strikes.short_put - strikes.long_put)
    pnl = _finite(fill.credit - settlement_loss, "pnl")
    close = _finite(close_price, "close_price")
    return DayEconomics(
        day=chain.day,
        regime=distances.regime,
        status=PricingStatus.PRICED,
        credit=fill.credit,
        max_loss=_finite(wing - fill.credit, "max_loss"),
        settlement_loss=settlement_loss,
        pnl=pnl,
        put_breached=close < strikes.short_put,
        call_breached=close > strikes.short_call,
    )


def _mean(values: Sequence[float]) -> float:
    return _finite(math.fsum(values), "mean") / len(values)


def _cvar_95(pnl_values: Sequence[float]) -> float:
    """Mean of the worst 5% of daily PnL (signed; negative values are losses)."""

    tail_count = max(1, math.ceil(len(pnl_values) * 0.05))
    worst = sorted(pnl_values)[:tail_count]
    return _mean(worst)


def _win_rate(pnl_values: Sequence[float]) -> float:
    return sum(1 for value in pnl_values if value > 0) / len(pnl_values)


def _max_daily_loss(pnl_values: Sequence[float]) -> float:
    return max(0.0, -min(pnl_values))


@dataclass(frozen=True, slots=True)
class RegimeEconomics:
    """Aggregate economics of one IV regime slice (priced days only)."""

    regime: str
    n_priced: int
    n_unpriceable: int
    total_pnl: float
    mean_pnl: float | None
    win_rate: float | None
    max_daily_loss: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "n_priced": self.n_priced,
            "n_unpriceable": self.n_unpriceable,
            "total_pnl": self.total_pnl,
            "mean_pnl": self.mean_pnl,
            "win_rate": self.win_rate,
            "max_daily_loss": self.max_daily_loss,
        }


@dataclass(frozen=True, slots=True)
class EconomicsSummary:
    """Aggregate economics over a batch of :class:`DayEconomics`.

    PnL aggregates cover PRICED days only; UNPRICEABLE days are counted and
    included in ``ev`` (expected value per *candidate* day, treating an
    unpriceable day as a zero-PnL no-trade). ``cvar_95`` is the mean of the
    worst 5% of priced daily PnL (signed). With no priced days the per-day
    statistics are None — never a fabricated zero.
    """

    n_priced: int
    n_unpriceable: int
    total_pnl: float
    mean_pnl: float | None
    ev: float
    cvar_95: float | None
    max_daily_loss: float | None
    win_rate: float | None
    regimes: Mapping[str, RegimeEconomics] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_priced": self.n_priced,
            "n_unpriceable": self.n_unpriceable,
            "total_pnl": self.total_pnl,
            "mean_pnl": self.mean_pnl,
            "ev": self.ev,
            "cvar_95": self.cvar_95,
            "max_daily_loss": self.max_daily_loss,
            "win_rate": self.win_rate,
            "regimes": {key: self.regimes[key].to_dict() for key in sorted(self.regimes)},
        }


def summarize(day_economics: Sequence[DayEconomics]) -> EconomicsSummary:
    """Aggregate per-day economics; UNPRICEABLE days are explicit, never dropped."""

    days = tuple(day_economics)
    priced = [day for day in days if day.status is PricingStatus.PRICED]
    pnl_values = tuple(day.pnl for day in priced if day.pnl is not None)
    total_pnl = _finite(math.fsum(pnl_values), "total_pnl") if pnl_values else 0.0
    n_candidates = len(days)
    ev = total_pnl / n_candidates if n_candidates else 0.0

    by_regime: dict[str, list[DayEconomics]] = defaultdict(list)
    for day in days:
        by_regime[day.regime].append(day)
    regimes: dict[str, RegimeEconomics] = {}
    for regime, members in sorted(by_regime.items()):
        regime_pnl = tuple(
            member.pnl
            for member in members
            if member.status is PricingStatus.PRICED and member.pnl is not None
        )
        regimes[regime] = RegimeEconomics(
            regime=regime,
            n_priced=len(regime_pnl),
            n_unpriceable=len(members) - len(regime_pnl),
            total_pnl=_finite(math.fsum(regime_pnl), "regime total_pnl")
            if regime_pnl
            else 0.0,
            mean_pnl=_mean(regime_pnl) if regime_pnl else None,
            win_rate=_win_rate(regime_pnl) if regime_pnl else None,
            max_daily_loss=_max_daily_loss(regime_pnl) if regime_pnl else None,
        )
    return EconomicsSummary(
        n_priced=len(pnl_values),
        n_unpriceable=len(days) - len(pnl_values),
        total_pnl=total_pnl,
        mean_pnl=_mean(pnl_values) if pnl_values else None,
        ev=_finite(ev, "ev"),
        cvar_95=_cvar_95(pnl_values) if pnl_values else None,
        max_daily_loss=_max_daily_loss(pnl_values) if pnl_values else None,
        win_rate=_win_rate(pnl_values) if pnl_values else None,
        regimes=MappingProxyType(regimes),
    )


@dataclass(frozen=True, slots=True)
class ExcursionLabel:
    """The label fields this workstream joins on (implied center/close)."""

    day: date
    entry_et: time
    entry_price: float
    close_price: float


def load_excursion_labels(path: str | Path) -> tuple[ExcursionLabel, ...]:
    """Read the workstream-C label store; extra provenance fields are ignored."""

    source = Path(path)
    labels: list[ExcursionLabel] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict) or not _LABEL_REQUIRED_KEYS.issubset(record):
            raise CondorEconomicsError(f"corrupt label record in {source}: {line!r}")
        labels.append(
            ExcursionLabel(
                day=date.fromisoformat(str(record["day"])),
                entry_et=time.fromisoformat(str(record["entry_et"])),
                entry_price=_finite(float(record["entry_price"]), "entry_price"),
                close_price=_finite(float(record["close_price"]), "close_price"),
            )
        )
    return tuple(labels)


def load_tail_distances(path: str | Path) -> tuple[TailDistances, ...]:
    """Read a distances JSONL shaped exactly as :class:`TailDistances` records.

    Records with missing or extra keys are rejected — the workstream-F
    contract is the frozen dataclass and nothing else.
    """

    source = Path(path)
    distances: list[TailDistances] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict) or set(record) != _DISTANCE_KEYS:
            raise CondorEconomicsError(f"corrupt distances record in {source}: {line!r}")
        distances.append(
            TailDistances(
                day=date.fromisoformat(str(record["day"])),
                down_distance=_finite(float(record["down_distance"]), "down_distance"),
                up_distance=_finite(float(record["up_distance"]), "up_distance"),
                regime=str(record["regime"]),
                model_version=str(record["model_version"]),
                quantile=_finite(float(record["quantile"]), "quantile"),
            )
        )
    return tuple(distances)


#: Injectable chain source (tests inject synthetic chains; production decodes
#: the landed DBN batches via :func:`normalize_day`).
type ChainLoader = Callable[[date], ChainDay]


@dataclass(frozen=True, slots=True)
class EconomicsBatchReport:
    """Aggregate result of one :func:`run_economics_batch` window."""

    start: date
    end: date
    summary: EconomicsSummary
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
            **self.summary.to_dict(),
        }


def run_economics_batch(
    *,
    labels_path: str | Path,
    distances_path: str | Path,
    data_root: str | Path,
    pit_ledger_path: str | Path,
    start: date,
    end: date,
    chain_loader: ChainLoader | None = None,
) -> EconomicsBatchReport:
    """Join labels, tail distances, and chains over ``[start, end]``.

    Days present in the distances file but not the labels are counted as
    ``n_missing_labels``; labelled days without a distance record are counted
    as ``n_missing_distances``; joined days whose chain cannot be normalized
    are counted as ``n_missing_chain``. All are explicit — no day is ever
    silently dropped.
    """

    if start > end:
        raise CondorEconomicsError(f"start {start} must not be after end {end}")

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
    economics: list[DayEconomics] = []
    for day in sorted(set(labels) | set(distances)):
        label = labels.get(day)
        record = distances.get(day)
        if label is None:
            n_missing_labels += 1
            continue
        if record is None:
            continue  # counted below, once
        entry = datetime.combine(day, label.entry_et, tzinfo=ET).astimezone(UTC)
        try:
            chain = loader(day)
        except Exception:  # NormalizeError or structural failure: explicit skip
            n_missing_chain += 1
            continue
        economics.append(
            evaluate_day(
                chain=chain,
                entry=entry,
                center=label.entry_price,
                distances=record,
                close_price=label.close_price,
            )
        )
    n_missing_distances = sum(1 for day in labels if day not in distances)
    return EconomicsBatchReport(
        start=start,
        end=end,
        summary=summarize(economics),
        n_missing_chain=n_missing_chain,
        n_missing_distances=n_missing_distances,
        n_missing_labels=n_missing_labels,
    )
