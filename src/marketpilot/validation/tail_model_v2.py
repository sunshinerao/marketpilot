"""Tail model v2: buffer-calibrated tail distances.

Follow-up to the v1 verdict (docs/development/calibration-report-v1.md): the
unconditional baseline covered 93.6% and the IV-regime model 86.3% out of
sample against a 97.5% target. V2 closes the gap honestly: distances are the
v1 empirical quantiles PLUS per-side additive buffers chosen on the TRAINING
window only as the smallest buffers whose joint corridor coverage reaches the
target. A window that cannot reach target coverage even at the maximum grid
buffer is UNCALIBRATED and the model abstains everywhere for that fold —
a knowingly-uncalibrated window is never shipped.

The headline model is :class:`BufferCalibratedTailModel` (unconditional).
:class:`BufferCalibratedIvModel` applies the same buffer logic per IV-quartile
regime with fallback to the unconditional buffered model, mirroring v1.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from types import MappingProxyType
from typing import ClassVar, Self

from marketpilot.validation.tail_calibration import empirical_quantile_higher
from marketpilot.validation.tail_distances import TailDistances, TailDistancesError
from marketpilot.validation.tail_model import (
    REGIME_CUTS,
    REGIME_NAMES,
    EntryFeatures,
    EvaluationMetrics,
    EvaluationReport,
    ExcursionLabel,
    IvRegimeTailModel,
    TailModelConfig,
    UnconditionalTailModel,
    _regime_name,
    walk_forward_evaluate,
)

DEFAULT_BUFFER_MAX = 100.0
DEFAULT_BUFFER_STEP = 2.0
BUFFERED_REGIME = "BUFFERED"

__all__ = [
    "BUFFERED_REGIME",
    "DEFAULT_BUFFER_MAX",
    "DEFAULT_BUFFER_STEP",
    "BufferCalibratedIvModel",
    "BufferCalibratedTailModel",
    "BufferCalibration",
    "V2Comparison",
    "V2ModelEfficiency",
    "buffer_grid",
    "evaluate_v2",
]


def buffer_grid(buffer_max: float, buffer_step: float) -> tuple[float, ...]:
    """Candidate buffer values: 0, step, 2*step, ... up to ``buffer_max``."""

    if not math.isfinite(buffer_max) or buffer_max < 0:
        raise TailDistancesError("buffer_max must be finite and non-negative")
    if not math.isfinite(buffer_step) or buffer_step <= 0:
        raise TailDistancesError("buffer_step must be positive and finite")
    steps = int(math.floor(buffer_max / buffer_step))
    return tuple(index * buffer_step for index in range(steps + 1))


@dataclass(frozen=True, slots=True)
class BufferCalibration:
    """Chosen per-side buffers for one quantile on one training window.

    ``train_coverage`` is the joint corridor coverage achieved on the training
    window at the chosen buffers; when ``calibrated`` is False the buffers are
    the grid maxima and ``train_coverage`` is the best coverage they reached
    (still below target). Uncalibrated models abstain; the buffers are kept
    only for diagnostics.
    """

    quantile: float
    target_coverage: float
    base_up: float
    base_down: float
    buffer_up: float
    buffer_down: float
    calibrated: bool
    train_coverage: float

    def __post_init__(self) -> None:
        if not 0 < self.quantile < 1:
            raise TailDistancesError("quantile must be within (0, 1)")
        if not 0 < self.target_coverage < 1:
            raise TailDistancesError("target_coverage must be within (0, 1)")
        for name, value in (
            ("base_up", self.base_up),
            ("base_down", self.base_down),
            ("buffer_up", self.buffer_up),
            ("buffer_down", self.buffer_down),
        ):
            if not math.isfinite(value) or value < 0:
                raise TailDistancesError(f"{name} must be finite and non-negative")
        if not 0.0 <= self.train_coverage <= 1.0:
            raise TailDistancesError("train_coverage must be in [0, 1]")
        if self.calibrated and self.train_coverage < self.target_coverage:
            raise TailDistancesError("calibrated buffers must reach target coverage")


def _calibrate_side_buffers(
    up_excess: Sequence[float],
    down_excess: Sequence[float],
    *,
    target_coverage: float,
    grid: tuple[float, ...],
) -> tuple[float, float, float] | None:
    """Smallest (buffer_up, buffer_down) reaching joint target coverage.

    "Smallest" minimizes the total buffer ``buffer_up + buffer_down``; ties
    break toward the smaller ``buffer_up`` then ``buffer_down``. Returns
    ``(buffer_up, buffer_down, achieved_coverage)`` or ``None`` when even the
    grid maxima cannot reach ``target_coverage``.
    """

    count = len(up_excess)
    best: tuple[float, float, float] | None = None
    best_key: tuple[float, float, float] | None = None
    for buffer_up in grid:
        up_ok = tuple(excess <= buffer_up for excess in up_excess)
        for buffer_down in grid:
            covered = sum(
                1
                for index in range(count)
                if up_ok[index] and down_excess[index] <= buffer_down
            )
            coverage = covered / count
            if coverage >= target_coverage:
                # First feasible buffer_down for this buffer_up is minimal;
                # larger values are dominated, so stop scanning this row.
                key = (buffer_up + buffer_down, buffer_up, buffer_down)
                if best_key is None or key < best_key:
                    best = (buffer_up, buffer_down, coverage)
                    best_key = key
                break
    return best


def _calibrate_quantile(
    labels: Sequence[ExcursionLabel],
    *,
    quantile: float,
    target_coverage: float,
    grid: tuple[float, ...],
) -> BufferCalibration:
    base_up = empirical_quantile_higher(tuple(label.up_max for label in labels), quantile)
    base_down = empirical_quantile_higher(
        tuple(label.down_max for label in labels), quantile
    )
    up_excess = tuple(label.up_max - base_up for label in labels)
    down_excess = tuple(label.down_max - base_down for label in labels)
    chosen = _calibrate_side_buffers(
        up_excess, down_excess, target_coverage=target_coverage, grid=grid
    )
    if chosen is None:
        max_buffer = grid[-1]
        covered = sum(
            1
            for index in range(len(labels))
            if up_excess[index] <= max_buffer and down_excess[index] <= max_buffer
        )
        return BufferCalibration(
            quantile=quantile,
            target_coverage=target_coverage,
            base_up=base_up,
            base_down=base_down,
            buffer_up=max_buffer,
            buffer_down=max_buffer,
            calibrated=False,
            train_coverage=covered / len(labels),
        )
    buffer_up, buffer_down, coverage = chosen
    return BufferCalibration(
        quantile=quantile,
        target_coverage=target_coverage,
        base_up=base_up,
        base_down=base_down,
        buffer_up=buffer_up,
        buffer_down=buffer_down,
        calibrated=True,
        train_coverage=coverage,
    )


def _validate_buffer_args(buffer_max: float, buffer_step: float) -> None:
    # Reuse grid validation so models fail fast at construction time.
    buffer_grid(buffer_max, buffer_step)


def _validate_target_coverage(target_coverage: float | None) -> None:
    if target_coverage is not None and not 0 < target_coverage < 1:
        raise TailDistancesError("target_coverage must be within (0, 1)")


@dataclass(frozen=True, slots=True)
class BufferCalibratedTailModel:
    """Unconditional empirical quantiles plus calibrated additive buffers.

    Buffers are fitted on the training window only (``fit`` never sees test
    days, matching the v1 leak discipline). When even ``buffer_max`` cannot
    lift training-window joint coverage to ``target_coverage`` (default: the
    quantile itself), the window is UNCALIBRATED and ``recommend`` abstains
    (returns ``None``) for every day.
    """

    config: TailModelConfig
    target_coverage: float | None = None
    buffer_max: float = DEFAULT_BUFFER_MAX
    buffer_step: float = DEFAULT_BUFFER_STEP
    _calibrations: Mapping[float, BufferCalibration] = field(default_factory=dict)
    _train_days: int = 0

    MODEL_VERSION: ClassVar[str] = "buffer-calibrated-tail-v2"

    def __post_init__(self) -> None:
        _validate_buffer_args(self.buffer_max, self.buffer_step)
        _validate_target_coverage(self.target_coverage)

    @property
    def model_version(self) -> str:
        return self.MODEL_VERSION

    @property
    def train_days(self) -> int:
        return self._train_days

    @property
    def calibrations(self) -> Mapping[float, BufferCalibration]:
        return self._calibrations

    def calibration(self, quantile: float) -> BufferCalibration:
        if quantile not in self._calibrations:
            raise TailDistancesError(f"quantile {quantile} was not fitted")
        return self._calibrations[quantile]

    def is_calibrated(self, quantile: float) -> bool:
        return self.calibration(quantile).calibrated

    def fit(
        self,
        labels: Sequence[ExcursionLabel],
        features: Sequence[EntryFeatures] = (),
    ) -> Self:
        if not labels:
            raise TailDistancesError("training labels must not be empty")
        grid = buffer_grid(self.buffer_max, self.buffer_step)
        calibrations = {
            quantile: _calibrate_quantile(
                labels,
                quantile=quantile,
                target_coverage=(
                    self.target_coverage
                    if self.target_coverage is not None
                    else quantile
                ),
                grid=grid,
            )
            for quantile in self.config.quantiles
        }
        return replace(
            self,
            _calibrations=MappingProxyType(calibrations),
            _train_days=len(labels),
        )

    def recommend(self, features: EntryFeatures, quantile: float) -> TailDistances | None:
        if not self._calibrations:
            raise TailDistancesError("model is not fitted")
        calibration = self.calibration(quantile)
        if not calibration.calibrated:
            return None  # UNCALIBRATED window -> NO_TRADE everywhere
        return TailDistances(
            day=features.day,
            down_distance=max(
                calibration.base_down + calibration.buffer_down,
                self.config.min_distance,
            ),
            up_distance=max(
                calibration.base_up + calibration.buffer_up,
                self.config.min_distance,
            ),
            regime=BUFFERED_REGIME,
            model_version=self.model_version,
            quantile=quantile,
        )


@dataclass(frozen=True, slots=True)
class BufferCalibratedIvModel:
    """Buffer-calibrated tails conditional on the training-day atm_iv quartile.

    Same buffer logic as :class:`BufferCalibratedTailModel` per IV-quartile
    regime. Regimes with fewer than ``config.min_regime_samples`` training
    days — or regimes whose buffers cannot reach target coverage — fall back
    to the unconditional buffered model (fitted on valid-IV training days)
    under a ``<REGIME>_BUFFERED_FALLBACK`` regime name, provided fallback is
    allowed and the fallback itself is calibrated; otherwise the model
    abstains (NO_TRADE).
    """

    config: TailModelConfig
    target_coverage: float | None = None
    buffer_max: float = DEFAULT_BUFFER_MAX
    buffer_step: float = DEFAULT_BUFFER_STEP
    _boundaries: tuple[float, ...] = ()
    _regime_models: Mapping[str, BufferCalibratedTailModel] = field(default_factory=dict)
    _regime_counts: Mapping[str, int] = field(default_factory=dict)
    _fallback: BufferCalibratedTailModel | None = None

    MODEL_VERSION: ClassVar[str] = "buffer-calibrated-iv-v2"

    def __post_init__(self) -> None:
        _validate_buffer_args(self.buffer_max, self.buffer_step)
        _validate_target_coverage(self.target_coverage)

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

    def _new_unconditional(self) -> BufferCalibratedTailModel:
        return BufferCalibratedTailModel(
            config=self.config,
            target_coverage=self.target_coverage,
            buffer_max=self.buffer_max,
            buffer_step=self.buffer_step,
        )

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
        fallback = self._new_unconditional().fit(tuple(label for label, _ in valid))
        buckets: dict[str, list[ExcursionLabel]] = {name: [] for name in REGIME_NAMES}
        for label, atm_iv in valid:
            buckets[_regime_name(boundaries, atm_iv)].append(label)
        models: dict[str, BufferCalibratedTailModel] = {}
        counts: dict[str, int] = {}
        for name in REGIME_NAMES:
            bucket = buckets[name]
            counts[name] = len(bucket)
            if len(bucket) >= self.config.min_regime_samples:
                models[name] = self._new_unconditional().fit(tuple(bucket))
        return replace(
            self,
            _boundaries=boundaries,
            _regime_models=MappingProxyType(models),
            _regime_counts=MappingProxyType(counts),
            _fallback=fallback,
        )

    def recommend(self, features: EntryFeatures, quantile: float) -> TailDistances | None:
        if not self._boundaries:
            raise TailDistancesError("model is not fitted")
        if quantile not in self.config.quantiles:
            raise TailDistancesError(f"quantile {quantile} was not fitted")
        if not features.atm_iv_valid or features.atm_iv is None:
            return None  # invalid features -> NO_TRADE
        name = _regime_name(self._boundaries, features.atm_iv)
        regime_model = self._regime_models.get(name)
        if regime_model is not None and regime_model.is_calibrated(quantile):
            calibrated = regime_model.calibration(quantile)
            return TailDistances(
                day=features.day,
                down_distance=max(
                    calibrated.base_down + calibrated.buffer_down,
                    self.config.min_distance,
                ),
                up_distance=max(
                    calibrated.base_up + calibrated.buffer_up,
                    self.config.min_distance,
                ),
                regime=f"{name}_BUFFERED",
                model_version=self.model_version,
                quantile=quantile,
            )
        # Underfit or uncalibrated regime -> unconditional buffered fallback.
        if (
            not self.config.allow_fallback
            or self._fallback is None
            or not self._fallback.is_calibrated(quantile)
        ):
            return None  # no calibrated fallback -> NO_TRADE
        fallback = self._fallback.recommend(features, quantile)
        if fallback is None:  # defensive: calibrated fallback never abstains
            return None
        return TailDistances(
            day=features.day,
            down_distance=fallback.down_distance,
            up_distance=fallback.up_distance,
            regime=f"{name}_BUFFERED_FALLBACK",
            model_version=self.model_version,
            quantile=quantile,
        )


# ---------------------------------------------------------------------------
# Three-way evaluation: buffered v2 vs both v1 models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class V2ModelEfficiency:
    """Coverage achieved per point of mean total corridor width.

    ``mean_total_distance`` is ``mean_up_distance + mean_down_distance`` over
    evaluated days. All fields are None when the model abstained on every
    test day (coverage is undefined there).
    """

    model_version: str
    coverage: float | None
    mean_total_distance: float | None
    coverage_per_distance: float | None

    def __post_init__(self) -> None:
        if not self.model_version.strip():
            raise ValueError("model_version must not be blank")


@dataclass(frozen=True, slots=True)
class V2Comparison:
    """Buffered v2 evaluated next to both v1 models in the same harness."""

    buffered: EvaluationReport
    unconditional: EvaluationReport
    iv_regime: EvaluationReport
    efficiency: tuple[V2ModelEfficiency, ...]


def _efficiency(report: EvaluationReport) -> V2ModelEfficiency:
    metrics: EvaluationMetrics = report.aggregate
    if (
        metrics.coverage is None
        or metrics.mean_up_distance is None
        or metrics.mean_down_distance is None
    ):
        return V2ModelEfficiency(
            model_version=report.model_version,
            coverage=None,
            mean_total_distance=None,
            coverage_per_distance=None,
        )
    mean_total = metrics.mean_up_distance + metrics.mean_down_distance
    return V2ModelEfficiency(
        model_version=report.model_version,
        coverage=metrics.coverage,
        mean_total_distance=mean_total,
        coverage_per_distance=metrics.coverage / mean_total,
    )


def evaluate_v2(
    labels: Iterable[ExcursionLabel | Mapping[str, object]],
    features: Iterable[EntryFeatures | Mapping[str, object]],
    *,
    config: TailModelConfig,
    quantile: float,
    target_coverage: float | None = None,
    folds: int,
    purge_gap: timedelta = timedelta(days=1),
) -> V2Comparison:
    """Run buffered v2, v1 unconditional, and v1 IV-regime in one harness.

    All three models go through the same purged walk-forward splitter with
    identical folds, so coverage and efficiency numbers are comparable. The
    buffered model defaults its per-quantile target coverage to the quantile
    itself when ``target_coverage`` is None.
    """

    _validate_target_coverage(target_coverage)
    label_list = list(labels)
    feature_list = list(features)
    buffered = walk_forward_evaluate(
        label_list,
        feature_list,
        model_factory=lambda: BufferCalibratedTailModel(
            config=config, target_coverage=target_coverage
        ),
        quantile=quantile,
        folds=folds,
        purge_gap=purge_gap,
    )
    unconditional = walk_forward_evaluate(
        label_list,
        feature_list,
        model_factory=lambda: UnconditionalTailModel(config=config),
        quantile=quantile,
        folds=folds,
        purge_gap=purge_gap,
    )
    iv_regime = walk_forward_evaluate(
        label_list,
        feature_list,
        model_factory=lambda: IvRegimeTailModel(config=config),
        quantile=quantile,
        folds=folds,
        purge_gap=purge_gap,
    )
    return V2Comparison(
        buffered=buffered,
        unconditional=unconditional,
        iv_regime=iv_regime,
        efficiency=(
            _efficiency(buffered),
            _efficiency(unconditional),
            _efficiency(iv_regime),
        ),
    )
