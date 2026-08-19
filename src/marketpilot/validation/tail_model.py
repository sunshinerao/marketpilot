"""Tail model v1: unconditional and IV-regime-conditional tail distances.

Workstream F of the calibration spine (docs/development/calibration-pipeline.md).
Both models emit the frozen ``TailDistances`` contract and are evaluated with
the existing purged walk-forward splitter. Abstention is explicit: a model that
cannot produce an honest recommendation returns ``None`` (NO_TRADE) instead of
silently trading.
"""

from __future__ import annotations

import math
import statistics
import tomllib
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Protocol, Self

from marketpilot.validation.tail_calibration import empirical_quantile_higher
from marketpilot.validation.tail_distances import TailDistances, TailDistancesError
from marketpilot.validation.walk_forward import PurgedWalkForwardSplitter, ValidationSample

DEFAULT_MIN_REGIME_SAMPLES = 20
DEFAULT_MIN_DISTANCE = 1e-6
REGIME_NAMES: tuple[str, ...] = ("IV_Q1", "IV_Q2", "IV_Q3", "IV_Q4")
REGIME_CUTS: tuple[float, ...] = (0.25, 0.5, 0.75)

__all__ = [
    "DEFAULT_MIN_DISTANCE",
    "DEFAULT_MIN_REGIME_SAMPLES",
    "REGIME_NAMES",
    "EntryFeatures",
    "EvaluationMetrics",
    "EvaluationReport",
    "ExcursionLabel",
    "FoldEvaluation",
    "IvRegimeTailModel",
    "ModelComparison",
    "TailModel",
    "TailModelConfig",
    "UnconditionalTailModel",
    "evaluate_against_unconditional_baseline",
    "load_tail_model_config",
    "walk_forward_evaluate",
]


# ---------------------------------------------------------------------------
# Input records (shapes produced by the label/feature workstreams)
# ---------------------------------------------------------------------------


def _parse_day(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise TailDistancesError(f"day is not an ISO date: {value!r}") from error
    raise TailDistancesError(f"day must be a date or ISO string, got {type(value).__name__}")


def _finite_nonnegative(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TailDistancesError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise TailDistancesError(f"{name} must be finite and non-negative")
    return result


@dataclass(frozen=True, slots=True)
class ExcursionLabel:
    """Realized entry→close max excursions for one day (excursions.jsonl shape)."""

    day: date
    up_max: float
    down_max: float
    entry_price: float | None = None

    def __post_init__(self) -> None:
        _finite_nonnegative(self.up_max, "up_max")
        _finite_nonnegative(self.down_max, "down_max")
        if self.entry_price is not None and _finite_nonnegative(
            self.entry_price, "entry_price"
        ) <= 0:
            raise TailDistancesError("entry_price must be positive")

    @classmethod
    def from_mapping(cls, record: Mapping[str, object]) -> ExcursionLabel:
        try:
            raw_day = record["day"]
            raw_up = record["up_max"]
            raw_down = record["down_max"]
        except KeyError as error:
            raise TailDistancesError(f"excursion label missing field {error}") from error
        raw_entry = record.get("entry_price")
        entry_price = (
            None if raw_entry is None else _finite_nonnegative(raw_entry, "entry_price")
        )
        return cls(
            day=_parse_day(raw_day),
            up_max=_finite_nonnegative(raw_up, "up_max"),
            down_max=_finite_nonnegative(raw_down, "down_max"),
            entry_price=entry_price,
        )


@dataclass(frozen=True, slots=True)
class EntryFeatures:
    """Candidate-entry features for one day (entry-features.jsonl shape).

    ``atm_iv_valid=False`` (or a missing ``atm_iv``) means the IV-regime model
    must abstain for the day.
    """

    day: date
    atm_iv: float | None
    atm_iv_valid: bool

    def __post_init__(self) -> None:
        if self.atm_iv is not None and _finite_nonnegative(self.atm_iv, "atm_iv") <= 0:
            raise TailDistancesError("atm_iv must be positive when present")

    @classmethod
    def from_mapping(cls, record: Mapping[str, object]) -> EntryFeatures:
        try:
            raw_day = record["day"]
        except KeyError as error:
            raise TailDistancesError(f"entry features missing field {error}") from error
        raw_iv = record.get("atm_iv")
        atm_iv = None if raw_iv is None else _finite_nonnegative(raw_iv, "atm_iv")
        raw_valid = record.get("atm_iv_valid", atm_iv is not None)
        if not isinstance(raw_valid, bool):
            raise TailDistancesError("atm_iv_valid must be a boolean")
        return cls(day=_parse_day(raw_day), atm_iv=atm_iv, atm_iv_valid=raw_valid)


def _coerce_label(record: ExcursionLabel | Mapping[str, object]) -> ExcursionLabel:
    if isinstance(record, ExcursionLabel):
        return record
    if isinstance(record, Mapping):
        return ExcursionLabel.from_mapping(record)
    raise TailDistancesError(f"unsupported label record: {type(record).__name__}")


def _coerce_features(record: EntryFeatures | Mapping[str, object]) -> EntryFeatures:
    if isinstance(record, EntryFeatures):
        return record
    if isinstance(record, Mapping):
        return EntryFeatures.from_mapping(record)
    raise TailDistancesError(f"unsupported feature record: {type(record).__name__}")


# ---------------------------------------------------------------------------
# Configuration (quantiles come from config/rules-v1.toml, read-only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TailModelConfig:
    """Quantile grid plus regime-fallback policy.

    ``min_distance`` is the contract floor applied to emitted distances:
    ``TailDistances`` requires strictly positive distances, while an empirical
    quantile of a zero-excursion window may be exactly 0. The default
    (1e-6 points) is economically irrelevant next to the 5-point strike
    increment and only ever raises a degenerate zero quantile.
    """

    quantiles: tuple[float, ...]
    min_regime_samples: int = DEFAULT_MIN_REGIME_SAMPLES
    allow_fallback: bool = True
    min_distance: float = DEFAULT_MIN_DISTANCE

    def __post_init__(self) -> None:
        if not self.quantiles:
            raise TailDistancesError("quantiles must not be empty")
        if any(not 0 < quantile < 1 for quantile in self.quantiles):
            raise TailDistancesError("quantiles must be within (0, 1)")
        if len(set(self.quantiles)) != len(self.quantiles):
            raise TailDistancesError("quantiles must be unique")
        object.__setattr__(self, "quantiles", tuple(sorted(self.quantiles)))
        if self.min_regime_samples < 1:
            raise TailDistancesError("min_regime_samples must be positive")
        if not math.isfinite(self.min_distance) or self.min_distance <= 0:
            raise TailDistancesError("min_distance must be positive and finite")


def load_tail_model_config(
    path: str | Path,
    *,
    min_regime_samples: int = DEFAULT_MIN_REGIME_SAMPLES,
    allow_fallback: bool = True,
) -> TailModelConfig:
    """Load the risk quantiles (normal/p1/p0) from a rules TOML, read-only."""

    with Path(path).open("rb") as stream:
        raw: Mapping[str, Any] = tomllib.load(stream)
    try:
        risk = raw["risk"]
        quantiles = (
            float(risk["normal_quantile"]),
            float(risk["p1_quantile"]),
            float(risk["p0_quantile"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TailDistancesError(f"rules file {path} lacks risk quantiles") from error
    return TailModelConfig(
        quantiles=quantiles,
        min_regime_samples=min_regime_samples,
        allow_fallback=allow_fallback,
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TailModel(Protocol):
    """Structural contract consumed by the walk-forward harness.

    ``fit`` returns a new fitted model (models are immutable). ``recommend``
    returns ``None`` when the model abstains; abstain days are NO_TRADE days
    and are never silently traded.
    """

    @property
    def model_version(self) -> str: ...

    def fit(
        self,
        labels: Sequence[ExcursionLabel],
        features: Sequence[EntryFeatures],
    ) -> Self: ...

    def recommend(self, features: EntryFeatures, quantile: float) -> TailDistances | None: ...


@dataclass(frozen=True, slots=True)
class UnconditionalTailModel:
    """Empirical up/down excursion quantiles over the whole training window."""

    config: TailModelConfig
    _up: Mapping[float, float] = field(default_factory=dict)
    _down: Mapping[float, float] = field(default_factory=dict)
    _train_days: int = 0

    MODEL_VERSION: ClassVar[str] = "unconditional-tail-v1"

    @property
    def model_version(self) -> str:
        return self.MODEL_VERSION

    @property
    def train_days(self) -> int:
        return self._train_days

    def fit(
        self,
        labels: Sequence[ExcursionLabel],
        features: Sequence[EntryFeatures] = (),
    ) -> Self:
        if not labels:
            raise TailDistancesError("training labels must not be empty")
        up = {
            quantile: empirical_quantile_higher(
                tuple(label.up_max for label in labels), quantile
            )
            for quantile in self.config.quantiles
        }
        down = {
            quantile: empirical_quantile_higher(
                tuple(label.down_max for label in labels), quantile
            )
            for quantile in self.config.quantiles
        }
        return replace(
            self,
            _up=MappingProxyType(up),
            _down=MappingProxyType(down),
            _train_days=len(labels),
        )

    def recommend(self, features: EntryFeatures, quantile: float) -> TailDistances | None:
        # The unconditional baseline needs no features; it never abstains.
        if not self._up:
            raise TailDistancesError("model is not fitted")
        if quantile not in self._up or quantile not in self._down:
            raise TailDistancesError(f"quantile {quantile} was not fitted")
        return TailDistances(
            day=features.day,
            down_distance=max(self._down[quantile], self.config.min_distance),
            up_distance=max(self._up[quantile], self.config.min_distance),
            regime="ALL",
            model_version=self.model_version,
            quantile=quantile,
        )


def _regime_name(boundaries: tuple[float, ...], atm_iv: float) -> str:
    return REGIME_NAMES[bisect_right(boundaries, atm_iv)]


@dataclass(frozen=True, slots=True)
class IvRegimeTailModel:
    """Empirical tail quantiles conditional on the training-day atm_iv quartile.

    Regime boundaries are empirical quartiles of the TRAINING window's atm_iv
    only; ``recommend`` never refits, so test-window data cannot leak into the
    boundaries. Regimes with fewer than ``config.min_regime_samples`` training
    days are unfit: with ``allow_fallback`` they emit the unconditional
    quantiles (over valid-IV training days) under a ``<REGIME>_FALLBACK``
    regime name; otherwise the model abstains (NO_TRADE).
    """

    config: TailModelConfig
    _boundaries: tuple[float, ...] = ()
    _regime_tables: Mapping[str, Mapping[float, tuple[float, float]]] = field(
        default_factory=dict
    )
    _regime_counts: Mapping[str, int] = field(default_factory=dict)
    _unconditional: UnconditionalTailModel | None = None

    MODEL_VERSION: ClassVar[str] = "iv-regime-tail-v1"

    @property
    def model_version(self) -> str:
        return self.MODEL_VERSION

    @property
    def regime_boundaries(self) -> tuple[float, ...]:
        """Training-window atm_iv quartile cut points (empty when unfitted)."""
        return self._boundaries

    @property
    def regime_sample_counts(self) -> Mapping[str, int]:
        return self._regime_counts

    def fit(
        self,
        labels: Sequence[ExcursionLabel],
        features: Sequence[EntryFeatures],
    ) -> Self:
        if not labels:
            raise TailDistancesError("training labels must not be empty")
        features_by_day = {feature.day: feature for feature in features}
        valid: list[tuple[ExcursionLabel, float]] = []
        for label in labels:
            feature = features_by_day.get(label.day)
            if feature is None or not feature.atm_iv_valid or feature.atm_iv is None:
                continue
            valid.append((label, feature.atm_iv))
        if not valid:
            raise TailDistancesError("no training days with valid atm_iv")
        boundaries = tuple(
            empirical_quantile_higher(tuple(atm_iv for _, atm_iv in valid), cut)
            for cut in REGIME_CUTS
        )
        fallback = UnconditionalTailModel(config=self.config).fit(
            tuple(label for label, _ in valid), ()
        )
        buckets: dict[str, list[ExcursionLabel]] = {name: [] for name in REGIME_NAMES}
        for label, atm_iv in valid:
            buckets[_regime_name(boundaries, atm_iv)].append(label)
        tables: dict[str, Mapping[float, tuple[float, float]]] = {}
        counts: dict[str, int] = {}
        for name in REGIME_NAMES:
            bucket = buckets[name]
            counts[name] = len(bucket)
            if len(bucket) >= self.config.min_regime_samples:
                tables[name] = MappingProxyType(
                    {
                        quantile: (
                            empirical_quantile_higher(
                                tuple(label.up_max for label in bucket), quantile
                            ),
                            empirical_quantile_higher(
                                tuple(label.down_max for label in bucket), quantile
                            ),
                        )
                        for quantile in self.config.quantiles
                    }
                )
        return replace(
            self,
            _boundaries=boundaries,
            _regime_tables=MappingProxyType(tables),
            _regime_counts=MappingProxyType(counts),
            _unconditional=fallback,
        )

    def recommend(self, features: EntryFeatures, quantile: float) -> TailDistances | None:
        if not self._boundaries:
            raise TailDistancesError("model is not fitted")
        if quantile not in self.config.quantiles:
            raise TailDistancesError(f"quantile {quantile} was not fitted")
        if not features.atm_iv_valid or features.atm_iv is None:
            return None  # invalid features -> NO_TRADE
        name = _regime_name(self._boundaries, features.atm_iv)
        table = self._regime_tables.get(name)
        if table is not None:
            up, down = table[quantile]
            return TailDistances(
                day=features.day,
                down_distance=max(down, self.config.min_distance),
                up_distance=max(up, self.config.min_distance),
                regime=name,
                model_version=self.model_version,
                quantile=quantile,
            )
        if not self.config.allow_fallback or self._unconditional is None:
            return None  # unfit regime with forbidden fallback -> NO_TRADE
        fallback = self._unconditional.recommend(features, quantile)
        if fallback is None:  # defensive: the unconditional model never abstains
            return None
        return TailDistances(
            day=features.day,
            down_distance=fallback.down_distance,
            up_distance=fallback.up_distance,
            regime=f"{name}_FALLBACK",
            model_version=self.model_version,
            quantile=quantile,
        )


# ---------------------------------------------------------------------------
# Purged walk-forward evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    """Corridor coverage and distance statistics over evaluated (non-abstain) days.

    Coverage is the share of evaluated days where the recommendation satisfies
    ``up_distance >= up_max`` AND ``down_distance >= down_max`` (joint corridor
    survival). Abstain (NO_TRADE) days are excluded from every coverage and
    distance statistic but reported via ``abstain_days`` / ``no_trade_share``.
    """

    test_days: int
    abstain_days: int
    evaluated_days: int
    covered_days: int
    up_breach_count: int
    down_breach_count: int
    coverage: float | None
    no_trade_share: float
    mean_up_distance: float | None
    mean_down_distance: float | None
    median_up_distance: float | None
    median_down_distance: float | None

    def __post_init__(self) -> None:
        counts = (
            self.test_days,
            self.abstain_days,
            self.evaluated_days,
            self.covered_days,
            self.up_breach_count,
            self.down_breach_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("evaluation counts must be non-negative")
        if self.abstain_days + self.evaluated_days != self.test_days:
            raise ValueError("abstain and evaluated days must add up to test days")
        if self.covered_days > self.evaluated_days:
            raise ValueError("covered days cannot exceed evaluated days")
        if self.up_breach_count > self.evaluated_days:
            raise ValueError("up breach count cannot exceed evaluated days")
        if self.down_breach_count > self.evaluated_days:
            raise ValueError("down breach count cannot exceed evaluated days")
        if not 0.0 <= self.no_trade_share <= 1.0:
            raise ValueError("no_trade_share must be in [0, 1]")
        if self.evaluated_days == 0:
            if self.coverage is not None:
                raise ValueError("coverage is undefined without evaluated days")
        elif self.coverage is None or not 0.0 <= self.coverage <= 1.0:
            raise ValueError("coverage must be in [0, 1] when days were evaluated")
        for value in (
            self.mean_up_distance,
            self.mean_down_distance,
            self.median_up_distance,
            self.median_down_distance,
        ):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError("distance statistics must be positive and finite")


@dataclass(frozen=True, slots=True)
class FoldEvaluation:
    fold_index: int
    train_days: int
    purge_cutoff: datetime
    test_start: datetime
    test_end: datetime
    metrics: EvaluationMetrics


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    model_version: str
    quantile: float
    purge_gap: timedelta
    folds: tuple[FoldEvaluation, ...]
    aggregate: EvaluationMetrics

    def __post_init__(self) -> None:
        if not self.model_version.strip():
            raise ValueError("model_version must not be blank")
        if not 0 < self.quantile < 1:
            raise ValueError("quantile must be within (0, 1)")
        if not self.folds:
            raise ValueError("an evaluation report needs at least one fold")


@dataclass(frozen=True, slots=True)
class ModelComparison:
    """Conditional model evaluated next to the unconditional baseline."""

    conditional: EvaluationReport
    baseline: EvaluationReport


def _build_metrics(
    *,
    test_days: int,
    abstain_days: int,
    covered_days: int,
    up_breach_count: int,
    down_breach_count: int,
    up_distances: Sequence[float],
    down_distances: Sequence[float],
) -> EvaluationMetrics:
    evaluated_days = test_days - abstain_days
    if evaluated_days != len(up_distances) or evaluated_days != len(down_distances):
        raise ValueError("distance observations must match evaluated days")
    return EvaluationMetrics(
        test_days=test_days,
        abstain_days=abstain_days,
        evaluated_days=evaluated_days,
        covered_days=covered_days,
        up_breach_count=up_breach_count,
        down_breach_count=down_breach_count,
        coverage=(covered_days / evaluated_days) if evaluated_days else None,
        no_trade_share=(abstain_days / test_days) if test_days else 0.0,
        mean_up_distance=statistics.fmean(up_distances) if up_distances else None,
        mean_down_distance=statistics.fmean(down_distances) if down_distances else None,
        median_up_distance=float(statistics.median(up_distances)) if up_distances else None,
        median_down_distance=(
            float(statistics.median(down_distances)) if down_distances else None
        ),
    )


def _invalid_features(day: date) -> EntryFeatures:
    return EntryFeatures(day=day, atm_iv=None, atm_iv_valid=False)


def walk_forward_evaluate(
    labels: Iterable[ExcursionLabel | Mapping[str, object]],
    features: Iterable[EntryFeatures | Mapping[str, object]],
    *,
    model_factory: Callable[[], TailModel],
    quantile: float,
    folds: int,
    min_train_days: int | None = None,
    purge_gap: timedelta = timedelta(days=1),
) -> EvaluationReport:
    """Evaluate a tail model with purged, embargoed expanding-window folds.

    Each calendar day is one validation group. ``model_factory`` must return a
    fresh unfitted model; per fold the model is fitted on the purged training
    window only and scored on the held-out test window. Days missing features
    are presented to the model as invalid, so the IV-regime model abstains on
    them. Defaults: ``min_train_days`` = half the days, ``purge_gap`` = 1 day
    embargo.
    """

    if not 0 < quantile < 1:
        raise ValueError("quantile must be within (0, 1)")
    if folds <= 0:
        raise ValueError("folds must be positive")
    if purge_gap < timedelta(0):
        raise ValueError("purge_gap must not be negative")

    labels_by_day: dict[date, ExcursionLabel] = {}
    for record in labels:
        label = _coerce_label(record)
        if label.day in labels_by_day:
            raise ValueError(f"duplicate excursion label for day {label.day}")
        labels_by_day[label.day] = label
    features_by_day: dict[date, EntryFeatures] = {}
    for feature_record in features:
        feature = _coerce_features(feature_record)
        if feature.day in features_by_day:
            raise ValueError(f"duplicate entry features for day {feature.day}")
        features_by_day[feature.day] = feature
    if not labels_by_day:
        raise ValueError("labels must not be empty")

    days = sorted(labels_by_day)
    total_days = len(days)
    min_train = min_train_days if min_train_days is not None else max(1, total_days // 2)
    if not 0 < min_train < total_days:
        raise ValueError("min_train_days must leave room for a test window")
    test_groups = max(1, (total_days - min_train) // folds)

    samples = tuple(
        ValidationSample(
            sample_id=day.isoformat(),
            observed_at=datetime.combine(day, time(12, 0), UTC),
            group_id=day.isoformat(),
            event_type="EXCURSION",
            regime="ALL",
        )
        for day in days
    )
    splitter = PurgedWalkForwardSplitter(
        min_train_groups=min_train,
        test_groups=test_groups,
        purge_gap=purge_gap,
    )
    walk_folds = splitter.split(samples)
    if not walk_folds:
        raise ValueError("no walk-forward folds; add days or reduce folds")

    fold_evaluations: list[FoldEvaluation] = []
    all_up_distances: list[float] = []
    all_down_distances: list[float] = []
    total_test = total_abstain = total_covered = 0
    total_up_breaches = total_down_breaches = 0
    model_version = ""
    for walk_fold in walk_folds:
        train_days = tuple(date.fromisoformat(sample_id) for sample_id in walk_fold.train_ids)
        test_days = tuple(date.fromisoformat(sample_id) for sample_id in walk_fold.test_ids)
        model = model_factory().fit(
            tuple(labels_by_day[day] for day in train_days),
            tuple(features_by_day.get(day) or _invalid_features(day) for day in train_days),
        )
        model_version = model.model_version
        covered = up_breaches = down_breaches = abstain = 0
        up_distances: list[float] = []
        down_distances: list[float] = []
        for day in test_days:
            feature = features_by_day.get(day) or _invalid_features(day)
            recommendation = model.recommend(feature, quantile)
            if recommendation is None:
                abstain += 1
                continue
            label = labels_by_day[day]
            if recommendation.up_distance < label.up_max:
                up_breaches += 1
            if recommendation.down_distance < label.down_max:
                down_breaches += 1
            if (
                recommendation.up_distance >= label.up_max
                and recommendation.down_distance >= label.down_max
            ):
                covered += 1
            up_distances.append(recommendation.up_distance)
            down_distances.append(recommendation.down_distance)
        metrics = _build_metrics(
            test_days=len(test_days),
            abstain_days=abstain,
            covered_days=covered,
            up_breach_count=up_breaches,
            down_breach_count=down_breaches,
            up_distances=up_distances,
            down_distances=down_distances,
        )
        fold_evaluations.append(
            FoldEvaluation(
                fold_index=walk_fold.fold_index,
                train_days=len(train_days),
                purge_cutoff=walk_fold.purge_cutoff,
                test_start=walk_fold.test_start,
                test_end=walk_fold.test_end,
                metrics=metrics,
            )
        )
        total_test += len(test_days)
        total_abstain += abstain
        total_covered += covered
        total_up_breaches += up_breaches
        total_down_breaches += down_breaches
        all_up_distances.extend(up_distances)
        all_down_distances.extend(down_distances)

    aggregate = _build_metrics(
        test_days=total_test,
        abstain_days=total_abstain,
        covered_days=total_covered,
        up_breach_count=total_up_breaches,
        down_breach_count=total_down_breaches,
        up_distances=all_up_distances,
        down_distances=all_down_distances,
    )
    return EvaluationReport(
        model_version=model_version,
        quantile=quantile,
        purge_gap=purge_gap,
        folds=tuple(fold_evaluations),
        aggregate=aggregate,
    )


def evaluate_against_unconditional_baseline(
    labels: Iterable[ExcursionLabel | Mapping[str, object]],
    features: Iterable[EntryFeatures | Mapping[str, object]],
    *,
    config: TailModelConfig,
    quantile: float,
    folds: int,
    min_train_days: int | None = None,
    purge_gap: timedelta = timedelta(days=1),
) -> ModelComparison:
    """Run the IV-regime model and the unconditional baseline in one harness."""

    label_list = list(labels)
    feature_list = list(features)
    conditional = walk_forward_evaluate(
        label_list,
        feature_list,
        model_factory=lambda: IvRegimeTailModel(config=config),
        quantile=quantile,
        folds=folds,
        min_train_days=min_train_days,
        purge_gap=purge_gap,
    )
    baseline = walk_forward_evaluate(
        label_list,
        feature_list,
        model_factory=lambda: UnconditionalTailModel(config=config),
        quantile=quantile,
        folds=folds,
        min_train_days=min_train_days,
        purge_gap=purge_gap,
    )
    return ModelComparison(conditional=conditional, baseline=baseline)
