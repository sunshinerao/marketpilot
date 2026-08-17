from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


def _finite(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


class ExecutionFailure(StrEnum):
    MISSING_LEGS = "MISSING_LEGS"
    QUOTE_IN_FUTURE = "QUOTE_IN_FUTURE"
    STALE_NBBO = "STALE_NBBO"
    INVALID_NBBO = "INVALID_NBBO"
    MISSING_SIZE = "MISSING_SIZE"
    INSUFFICIENT_SIZE = "INSUFFICIENT_SIZE"
    LIQUIDITY_LIMIT = "LIQUIDITY_LIMIT"
    LEG_SET_MISMATCH = "LEG_SET_MISMATCH"


@dataclass(frozen=True, slots=True)
class ExecutionAssumptions:
    max_quote_age: timedelta
    fee_per_contract: float
    slippage_per_contract: float
    max_size_participation: float = 1.0

    def __post_init__(self) -> None:
        if self.max_quote_age < timedelta(0):
            raise ValueError("max_quote_age must not be negative")
        fee = _finite(self.fee_per_contract, "fee_per_contract")
        slippage = _finite(self.slippage_per_contract, "slippage_per_contract")
        participation = _finite(self.max_size_participation, "max_size_participation")
        if fee < 0 or slippage < 0:
            raise ValueError("fees and slippage must not be negative")
        if not 0 < participation <= 1:
            raise ValueError("max_size_participation must be in (0, 1]")
        object.__setattr__(self, "fee_per_contract", fee)
        object.__setattr__(self, "slippage_per_contract", slippage)
        object.__setattr__(self, "max_size_participation", participation)


@dataclass(frozen=True, slots=True)
class LegNbbo:
    leg_id: str
    quantity: int
    multiplier: float
    bid: float
    ask: float
    bid_size: int | None
    ask_size: int | None
    quoted_at: datetime

    def __post_init__(self) -> None:
        if not self.leg_id.strip():
            raise ValueError("leg_id must not be blank")
        if self.quantity == 0:
            raise ValueError("quantity must not be zero")
        multiplier = _finite(self.multiplier, "multiplier")
        bid = _finite(self.bid, "bid")
        ask = _finite(self.ask, "ask")
        if multiplier <= 0:
            raise ValueError("multiplier must be positive")
        # Crossed or negative markets never have a defensible executable value.
        if bid < 0 or ask < bid:
            raise ValueError("require 0 <= bid <= ask")
        if self.bid_size is not None and self.bid_size < 0:
            raise ValueError("bid_size must not be negative")
        if self.ask_size is not None and self.ask_size < 0:
            raise ValueError("ask_size must not be negative")
        object.__setattr__(self, "multiplier", multiplier)
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "quoted_at", _utc(self.quoted_at, "quoted_at"))


@dataclass(frozen=True, slots=True)
class ExecutedLeg:
    leg_id: str
    quantity: int
    execution_price: float
    half_spread_cost: float
    slippage_cost: float
    fee: float


@dataclass(frozen=True, slots=True)
class ExecutableValue:
    valued_at: datetime
    is_executable: bool
    failures: tuple[ExecutionFailure, ...]
    legs: tuple[ExecutedLeg, ...] = ()
    gross_cashflow: float | None = None
    fees: float | None = None
    net_cashflow: float | None = None
    net_credit: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "valued_at", _utc(self.valued_at, "valued_at"))
        for name in ("gross_cashflow", "fees", "net_cashflow", "net_credit"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _finite(value, name))


def value_opening_execution(
    quotes: tuple[LegNbbo, ...],
    *,
    valued_at: datetime,
    assumptions: ExecutionAssumptions,
) -> ExecutableValue:
    """Value a simultaneous opening fill at conservative NBBO sides.

    Positive quantity buys at ask plus slippage; negative quantity sells at bid
    minus slippage. If any leg lacks fresh, sufficient displayed size, all exact
    leg and credit fields are suppressed.
    """

    return _value(quotes, valued_at=valued_at, assumptions=assumptions, closing=False)


def _value(
    quotes: tuple[LegNbbo, ...],
    *,
    valued_at: datetime,
    assumptions: ExecutionAssumptions,
    closing: bool,
) -> ExecutableValue:
    now = _utc(valued_at, "valued_at")
    failures: set[ExecutionFailure] = set()
    if not quotes:
        failures.add(ExecutionFailure.MISSING_LEGS)
    if len({quote.leg_id for quote in quotes}) != len(quotes):
        raise ValueError("leg_id values must be unique")
    for quote in quotes:
        if quote.quoted_at > now:
            failures.add(ExecutionFailure.QUOTE_IN_FUTURE)
        elif now - quote.quoted_at > assumptions.max_quote_age:
            failures.add(ExecutionFailure.STALE_NBBO)
        trade_quantity = -quote.quantity if closing else quote.quantity
        displayed_size = quote.ask_size if trade_quantity > 0 else quote.bid_size
        if displayed_size is None:
            failures.add(ExecutionFailure.MISSING_SIZE)
        elif displayed_size < abs(trade_quantity):
            failures.add(ExecutionFailure.INSUFFICIENT_SIZE)
        elif abs(trade_quantity) / displayed_size > assumptions.max_size_participation:
            failures.add(ExecutionFailure.LIQUIDITY_LIMIT)
    if failures:
        return ExecutableValue(
            valued_at=now,
            is_executable=False,
            failures=tuple(sorted(failures, key=str)),
        )

    legs: list[ExecutedLeg] = []
    gross_cashflow = 0.0
    total_fees = 0.0
    for quote in quotes:
        trade_quantity = -quote.quantity if closing else quote.quantity
        side_price = quote.ask if trade_quantity > 0 else quote.bid
        price = side_price + (
            assumptions.slippage_per_contract
            if trade_quantity > 0
            else -assumptions.slippage_per_contract
        )
        if price < 0:
            failures.add(ExecutionFailure.INVALID_NBBO)
            continue
        contracts = abs(trade_quantity)
        midpoint = (quote.bid + quote.ask) / 2
        half_spread_cost = abs(side_price - midpoint) * contracts * quote.multiplier
        slippage_cost = assumptions.slippage_per_contract * contracts * quote.multiplier
        fee = assumptions.fee_per_contract * contracts
        gross_cashflow += -trade_quantity * price * quote.multiplier
        total_fees += fee
        legs.append(
            ExecutedLeg(
                leg_id=quote.leg_id,
                quantity=trade_quantity,
                execution_price=price,
                half_spread_cost=half_spread_cost,
                slippage_cost=slippage_cost,
                fee=fee,
            )
        )
    if failures:
        return ExecutableValue(
            valued_at=now,
            is_executable=False,
            failures=tuple(sorted(failures, key=str)),
        )
    net_cashflow = _finite(gross_cashflow - total_fees, "net_cashflow")
    return ExecutableValue(
        valued_at=now,
        is_executable=True,
        failures=(),
        legs=tuple(legs),
        gross_cashflow=_finite(gross_cashflow, "gross_cashflow"),
        fees=_finite(total_fees, "fees"),
        net_cashflow=net_cashflow,
        net_credit=max(0.0, net_cashflow),
    )


@dataclass(frozen=True, slots=True)
class ConservativeExecutablePnl:
    opened_at: datetime
    closed_at: datetime
    opening_net_cashflow: float
    closing_net_cashflow: float
    pnl: float

    def __post_init__(self) -> None:
        opened = _utc(self.opened_at, "opened_at")
        closed = _utc(self.closed_at, "closed_at")
        if closed <= opened:
            raise ValueError("closed_at must be after opened_at")
        object.__setattr__(self, "opened_at", opened)
        object.__setattr__(self, "closed_at", closed)
        for name in ("opening_net_cashflow", "closing_net_cashflow", "pnl"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))


def conservative_executable_pnl(
    opening_quotes: tuple[LegNbbo, ...],
    closing_quotes: tuple[LegNbbo, ...],
    *,
    opened_at: datetime,
    closed_at: datetime,
    assumptions: ExecutionAssumptions,
) -> ConservativeExecutablePnl | None:
    """Return fee/spread/slippage-aware PnL, or None when either fill is unprovable."""

    opened = _utc(opened_at, "opened_at")
    closed = _utc(closed_at, "closed_at")
    if closed <= opened:
        raise ValueError("closed_at must be after opened_at")
    opening_positions = {
        quote.leg_id: (quote.quantity, quote.multiplier) for quote in opening_quotes
    }
    closing_positions = {
        quote.leg_id: (quote.quantity, quote.multiplier) for quote in closing_quotes
    }
    if opening_positions != closing_positions:
        return None
    opening = _value(
        opening_quotes,
        valued_at=opened,
        assumptions=assumptions,
        closing=False,
    )
    closing = _value(
        closing_quotes,
        valued_at=closed,
        assumptions=assumptions,
        closing=True,
    )
    if not opening.is_executable or not closing.is_executable:
        return None
    assert opening.net_cashflow is not None and closing.net_cashflow is not None
    pnl = opening.net_cashflow + closing.net_cashflow
    if not math.isfinite(pnl):
        raise ValueError("calculated pnl must be finite")
    return ConservativeExecutablePnl(
        opened_at=opened,
        closed_at=closed,
        opening_net_cashflow=opening.net_cashflow,
        closing_net_cashflow=closing.net_cashflow,
        pnl=pnl,
    )


@dataclass(frozen=True, slots=True)
class PnlMarkQuotes:
    marked_at: datetime
    quotes: tuple[LegNbbo, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "marked_at", _utc(self.marked_at, "marked_at"))


@dataclass(frozen=True, slots=True)
class ConservativePnlPath:
    marks: tuple[ConservativeExecutablePnl, ...]
    maximum_mtm_loss: float


def evaluate_conservative_pnl_path(
    opening_quotes: tuple[LegNbbo, ...],
    marks: tuple[PnlMarkQuotes, ...],
    *,
    opened_at: datetime,
    assumptions: ExecutionAssumptions,
) -> ConservativePnlPath | None:
    """Value executable MTM closes, suppressing the entire path on any quote gap."""

    opened = _utc(opened_at, "opened_at")
    if not marks:
        raise ValueError("marks must not be empty")
    if any(mark.marked_at <= opened for mark in marks):
        raise ValueError("marks must be strictly after opened_at")
    if any(
        left.marked_at >= right.marked_at for left, right in zip(marks, marks[1:], strict=False)
    ):
        raise ValueError("marks must be strictly time ordered")
    calculated: list[ConservativeExecutablePnl] = []
    for mark in marks:
        pnl = conservative_executable_pnl(
            opening_quotes,
            mark.quotes,
            opened_at=opened,
            closed_at=mark.marked_at,
            assumptions=assumptions,
        )
        if pnl is None:
            return None
        calculated.append(pnl)
    return ConservativePnlPath(
        marks=tuple(calculated),
        maximum_mtm_loss=max(0.0, -min(item.pnl for item in calculated)),
    )
