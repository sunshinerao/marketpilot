from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from marketpilot.domain.decision import DecisionAction


def _finite_sum(values: Sequence[float], name: str) -> float:
    try:
        result = math.fsum(values)
    except OverflowError as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class ValidationResult:
    sample_id: str
    event_type: str
    regime: str
    action: DecisionAction
    counterfactual_pnl: float
    realized_pnl: float | None
    metrics: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.sample_id.strip():
            raise ValueError("sample_id must not be blank")
        if not math.isfinite(self.counterfactual_pnl):
            raise ValueError("counterfactual_pnl must be finite")
        if self.realized_pnl is not None and not math.isfinite(self.realized_pnl):
            raise ValueError("realized_pnl must be finite")
        if any(not math.isfinite(value) for value in self.metrics.values()):
            raise ValueError("metrics must contain only finite values")
        if self.action is DecisionAction.ENTER and self.realized_pnl is None:
            raise ValueError("ENTER requires realized_pnl")
        if self.action is not DecisionAction.ENTER and self.realized_pnl is not None:
            raise ValueError("WAIT and NO_TRADE must not claim a realized strategy PnL")
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


def conditional_value_at_risk(
    pnl_observations: Sequence[float], *, confidence: float = 0.95
) -> float:
    """Empirical loss CVaR of a PnL series (positive values represent loss)."""

    if not pnl_observations:
        raise ValueError("pnl_observations must not be empty")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    values = tuple(float(value) for value in pnl_observations)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("pnl_observations must be finite")
    tail_count = max(1, math.ceil(len(values) * (1 - confidence)))
    worst = sorted(values)[:tail_count]
    return max(0.0, -_finite_sum(worst, "tail pnl") / len(worst))


def maximum_drawdown(pnl_increments: Sequence[float]) -> float:
    """Maximum peak-to-trough drawdown of cumulative PnL, starting at zero."""

    values = tuple(float(value) for value in pnl_increments)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("pnl_increments must be finite")
    equity = 0.0
    peak = 0.0
    maximum = 0.0
    for value in values:
        equity += value
        if not math.isfinite(equity):
            raise ValueError("cumulative pnl must be finite")
        peak = max(peak, equity)
        drawdown = peak - equity
        if not math.isfinite(drawdown):
            raise ValueError("drawdown must be finite")
        maximum = max(maximum, drawdown)
    return maximum


@dataclass(frozen=True, slots=True)
class NoTradeEffect:
    eligible_count: int
    no_trade_count: int
    entered_count: int
    filtered_pnl_total: float
    unfiltered_counterfactual_pnl_total: float
    no_trade_counterfactual_pnl_total: float
    pnl_difference: float


@dataclass(frozen=True, slots=True)
class ValidationSliceSummary:
    strata: tuple[tuple[str, str], ...]
    sample_count: int
    action_counts: Mapping[DecisionAction, int]
    metric_means: Mapping[str, float]
    no_trade_effect: NoTradeEffect


def _summarize_slice(
    strata: tuple[tuple[str, str], ...],
    results: Sequence[ValidationResult],
) -> ValidationSliceSummary:
    action_counts = Counter(result.action for result in results)
    metric_values: dict[str, list[float]] = defaultdict(list)
    for result in results:
        for name, value in result.metrics.items():
            metric_values[name].append(value)

    filtered_pnl = _finite_sum(
        tuple(result.realized_pnl or 0.0 for result in results), "filtered pnl"
    )
    unfiltered_pnl = _finite_sum(
        tuple(result.counterfactual_pnl for result in results), "unfiltered pnl"
    )
    no_trade_counterfactual = _finite_sum(
        tuple(
            result.counterfactual_pnl
            for result in results
            if result.action is DecisionAction.NO_TRADE
        ),
        "no-trade counterfactual pnl",
    )
    no_trade_count = action_counts[DecisionAction.NO_TRADE]
    pnl_difference = filtered_pnl - unfiltered_pnl
    if not math.isfinite(pnl_difference):
        raise ValueError("no-trade pnl difference must be finite")
    effect = NoTradeEffect(
        eligible_count=len(results),
        no_trade_count=no_trade_count,
        entered_count=action_counts[DecisionAction.ENTER],
        filtered_pnl_total=filtered_pnl,
        unfiltered_counterfactual_pnl_total=unfiltered_pnl,
        no_trade_counterfactual_pnl_total=no_trade_counterfactual,
        pnl_difference=pnl_difference,
    )
    means = {
        name: _finite_sum(values, f"{name} metric") / len(values)
        for name, values in metric_values.items()
    }
    return ValidationSliceSummary(
        strata=strata,
        sample_count=len(results),
        action_counts=MappingProxyType(dict(action_counts)),
        metric_means=MappingProxyType(means),
        no_trade_effect=effect,
    )


def summarize_validation(
    results: Sequence[ValidationResult],
    *,
    dimensions: tuple[str, ...] = ("event_type", "regime"),
) -> tuple[ValidationSliceSummary, ...]:
    allowed = {"event_type", "regime"}
    if not dimensions or not set(dimensions) <= allowed:
        raise ValueError("dimensions must contain event_type and/or regime")
    grouped: dict[tuple[str, ...], list[ValidationResult]] = defaultdict(list)
    for result in results:
        grouped[tuple(str(getattr(result, dimension)) for dimension in dimensions)].append(result)
    return tuple(
        _summarize_slice(tuple(zip(dimensions, key, strict=True)), grouped[key])
        for key in sorted(grouped)
    )
