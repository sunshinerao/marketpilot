"""Hermetic tests for tail model v2: buffer-calibrated tail distances.

All data is synthetic; no data/raw, no network. The tests pin the calibration
semantics: smallest buffers reaching target joint coverage, zero-buffer
equivalence with v1, total abstention on UNCALIBRATED windows, train-only
buffer computation (no test-day leakage), regime fallback, and grid edges.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from marketpilot.validation.tail_distances import TailDistancesError
from marketpilot.validation.tail_model import (
    EntryFeatures,
    ExcursionLabel,
    TailModelConfig,
    UnconditionalTailModel,
    walk_forward_evaluate,
)
from marketpilot.validation.tail_model_v2 import (
    BufferCalibratedIvModel,
    BufferCalibratedTailModel,
    buffer_grid,
    evaluate_v2,
)

DAY_ZERO = date(2026, 1, 5)
QUANTILE = 0.95


def make_day(index: int) -> date:
    return DAY_ZERO + timedelta(days=index)


def make_labels(
    excursions: list[tuple[float, float]], *, start: int = 0
) -> list[ExcursionLabel]:
    return [
        ExcursionLabel(day=make_day(start + index), up_max=up, down_max=down)
        for index, (up, down) in enumerate(excursions)
    ]


def make_features(count: int, *, start: int = 0, atm_iv: float = 0.15) -> list[EntryFeatures]:
    return [
        EntryFeatures(day=make_day(start + index), atm_iv=atm_iv, atm_iv_valid=True)
        for index in range(count)
    ]


def make_config(**overrides: object) -> TailModelConfig:
    kwargs: dict[str, object] = {"quantiles": (QUANTILE,)}
    kwargs.update(overrides)
    return TailModelConfig(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Buffer grid
# ---------------------------------------------------------------------------


def test_buffer_grid_shape_and_validation() -> None:
    assert buffer_grid(100.0, 2.0) == tuple(2.0 * index for index in range(51))
    assert buffer_grid(0.0, 2.0) == (0.0,)
    assert buffer_grid(3.0, 2.0) == (0.0, 2.0)  # stays at or below buffer_max
    with pytest.raises(TailDistancesError, match="buffer_step"):
        buffer_grid(10.0, 0.0)
    with pytest.raises(TailDistancesError, match="buffer_max"):
        buffer_grid(-1.0, 2.0)


def test_model_rejects_invalid_buffer_or_target_args() -> None:
    config = make_config()
    with pytest.raises(TailDistancesError, match="buffer_step"):
        BufferCalibratedTailModel(config=config, buffer_step=-2.0)
    with pytest.raises(TailDistancesError, match="target_coverage"):
        BufferCalibratedTailModel(config=config, target_coverage=1.5)
    with pytest.raises(TailDistancesError, match="target_coverage"):
        BufferCalibratedIvModel(config=config, target_coverage=0.0)


# ---------------------------------------------------------------------------
# Calibration semantics
# ---------------------------------------------------------------------------


def test_buffer_picks_smallest_value_reaching_target() -> None:
    """95 flat days plus a known tail: buffer 6 exactly reaches 98% coverage."""

    excursions = [(10.0, 10.0)] * 95 + [
        (12.0, 12.0),
        (14.0, 14.0),
        (16.0, 16.0),
        (18.0, 18.0),
        (20.0, 20.0),
    ]
    model = BufferCalibratedTailModel(config=make_config(), target_coverage=0.98).fit(
        make_labels(excursions)
    )
    calibration = model.calibration(QUANTILE)
    assert calibration.calibrated is True
    # Base is the 95th-smallest excursion; buffer 4 covers only 97% (< target).
    assert calibration.base_up == 10.0
    assert calibration.base_down == 10.0
    assert (calibration.buffer_up, calibration.buffer_down) == (6.0, 6.0)
    assert calibration.train_coverage == pytest.approx(0.98)

    recommendation = model.recommend(
        EntryFeatures(day=make_day(500), atm_iv=None, atm_iv_valid=False), QUANTILE
    )
    assert recommendation is not None
    assert recommendation.regime == "BUFFERED"
    assert recommendation.model_version == "buffer-calibrated-tail-v2"
    assert recommendation.up_distance == pytest.approx(16.0)
    assert recommendation.down_distance == pytest.approx(16.0)


def test_zero_buffer_matches_v1_when_quantile_already_covers() -> None:
    """When the raw quantile already meets target coverage, v2 emits v1 distances."""

    excursions = [(5.0, 5.0)] * 39 + [(100.0, 100.0)]
    labels = make_labels(excursions)
    features = EntryFeatures(day=make_day(100), atm_iv=None, atm_iv_valid=False)

    v2 = BufferCalibratedTailModel(config=make_config()).fit(labels)
    v1 = UnconditionalTailModel(config=make_config()).fit(labels)

    calibration = v2.calibration(QUANTILE)
    assert calibration.calibrated is True
    assert (calibration.buffer_up, calibration.buffer_down) == (0.0, 0.0)
    assert calibration.train_coverage == pytest.approx(39 / 40)

    rec_v2 = v2.recommend(features, QUANTILE)
    rec_v1 = v1.recommend(features, QUANTILE)
    assert rec_v2 is not None and rec_v1 is not None
    assert rec_v2.up_distance == rec_v1.up_distance
    assert rec_v2.down_distance == rec_v1.down_distance
    assert rec_v2.regime == "BUFFERED"
    assert rec_v1.regime == "ALL"


def test_uncalibrated_window_abstains_for_every_day() -> None:
    """Target unreachable within the grid -> UNCALIBRATED -> total abstention."""

    excursions = [(10.0, 10.0)] * 95 + [(2000.0, 2000.0)] * 5
    labels = make_labels(excursions)
    model = BufferCalibratedTailModel(config=make_config(), target_coverage=0.99).fit(labels)

    calibration = model.calibration(QUANTILE)
    assert calibration.calibrated is False
    assert (calibration.buffer_up, calibration.buffer_down) == (100.0, 100.0)
    assert calibration.train_coverage == pytest.approx(0.95)

    for index in range(len(labels) + 10):  # train days and unseen later days
        feature = EntryFeatures(day=make_day(index), atm_iv=0.2, atm_iv_valid=True)
        assert model.recommend(feature, QUANTILE) is None


def test_small_grid_can_force_uncalibrated() -> None:
    """Same data as the smallest-buffer test, but the grid stops below the need."""

    excursions = [(10.0, 10.0)] * 95 + [(20.0, 20.0)] * 5
    model = BufferCalibratedTailModel(
        config=make_config(), target_coverage=0.98, buffer_max=4.0, buffer_step=2.0
    ).fit(make_labels(excursions))
    calibration = model.calibration(QUANTILE)
    assert calibration.calibrated is False
    assert (calibration.buffer_up, calibration.buffer_down) == (4.0, 4.0)
    feature = EntryFeatures(day=make_day(200), atm_iv=None, atm_iv_valid=False)
    assert model.recommend(feature, QUANTILE) is None


# ---------------------------------------------------------------------------
# Walk-forward integration + leakage guard
# ---------------------------------------------------------------------------


def test_walk_forward_uncalibrated_window_reports_total_abstention() -> None:
    # ~3% spike days placed away from fold/purge boundaries: the 0.95 quantile
    # stays below the spikes, so even the max buffer covers only ~96.7% of any
    # training window (< 0.99 target).
    excursions = [
        (5000.0, 5000.0) if index % 30 == 5 else (10.0, 10.0) for index in range(60)
    ]
    report = walk_forward_evaluate(
        make_labels(excursions),
        make_features(60),
        model_factory=lambda: BufferCalibratedTailModel(
            config=make_config(), target_coverage=0.99
        ),
        quantile=QUANTILE,
        folds=2,
        min_train_days=30,
    )
    assert report.model_version == "buffer-calibrated-tail-v2"
    assert report.aggregate.evaluated_days == 0
    assert report.aggregate.abstain_days == report.aggregate.test_days
    assert report.aggregate.no_trade_share == 1.0
    assert report.aggregate.coverage is None


def test_buffers_are_computed_without_test_days() -> None:
    """Fitting on the training window only must ignore later (test-day) spikes."""

    train_excursions = [(5.0, 5.0)] * 49 + [(12.0, 12.0)]
    test_excursions = [(800.0, 800.0)] * 10
    train_model = BufferCalibratedTailModel(config=make_config()).fit(
        make_labels(train_excursions)
    )
    full_model = BufferCalibratedTailModel(config=make_config()).fit(
        make_labels(train_excursions + test_excursions)
    )

    train_calibration = train_model.calibration(QUANTILE)
    # Analytic expectation from the 50 training days only: base is the 48th
    # smallest (5.0) and zero buffer already covers 49/50 >= 0.95.
    assert train_calibration.base_up == 5.0
    assert train_calibration.base_down == 5.0
    assert (train_calibration.buffer_up, train_calibration.buffer_down) == (0.0, 0.0)

    full_calibration = full_model.calibration(QUANTILE)
    # Had the test days leaked into fitting, the base would jump to the spikes.
    assert full_calibration.base_up == 800.0
    assert full_calibration.base_up > train_calibration.base_up

    feature = EntryFeatures(day=make_day(100), atm_iv=None, atm_iv_valid=False)
    train_rec = train_model.recommend(feature, QUANTILE)
    full_rec = full_model.recommend(feature, QUANTILE)
    assert train_rec is not None and full_rec is not None
    assert train_rec.up_distance < full_rec.up_distance


# ---------------------------------------------------------------------------
# IV-regime buffered model
# ---------------------------------------------------------------------------


def make_two_cluster_dataset() -> tuple[list[ExcursionLabel], list[EntryFeatures]]:
    """14 low-IV days (narrow) + 6 high-IV days (wide) -> Q3 and Q4 buckets."""

    labels: list[ExcursionLabel] = []
    features: list[EntryFeatures] = []
    for index in range(14):
        day = make_day(index)
        labels.append(ExcursionLabel(day=day, up_max=5.0, down_max=5.0))
        features.append(EntryFeatures(day=day, atm_iv=0.10, atm_iv_valid=True))
    for index in range(14, 20):
        day = make_day(index)
        labels.append(ExcursionLabel(day=day, up_max=50.0, down_max=50.0))
        features.append(EntryFeatures(day=day, atm_iv=0.30, atm_iv_valid=True))
    return labels, features


def test_regime_fallback_for_underfit_regimes() -> None:
    labels, features = make_two_cluster_dataset()
    config = make_config(min_regime_samples=10)
    model = BufferCalibratedIvModel(config=config).fit(labels, features)

    # Empirical quartile cuts send 0.10 -> IV_Q3 (14 days, fit) and
    # 0.30 -> IV_Q4 (6 days, underfit -> fallback).
    assert model.regime_sample_counts["IV_Q3"] == 14
    assert model.regime_sample_counts["IV_Q4"] == 6

    in_regime = model.recommend(
        EntryFeatures(day=make_day(100), atm_iv=0.10, atm_iv_valid=True), QUANTILE
    )
    assert in_regime is not None
    assert in_regime.regime == "IV_Q3_BUFFERED"
    assert in_regime.up_distance == pytest.approx(5.0)

    fallback = model.recommend(
        EntryFeatures(day=make_day(101), atm_iv=0.30, atm_iv_valid=True), QUANTILE
    )
    assert fallback is not None
    assert fallback.regime == "IV_Q4_BUFFERED_FALLBACK"
    # Fallback is the unconditional buffered model over all valid-IV days.
    assert fallback.up_distance == pytest.approx(50.0)

    unseen_bucket = model.recommend(
        EntryFeatures(day=make_day(102), atm_iv=0.05, atm_iv_valid=True), QUANTILE
    )
    assert unseen_bucket is not None
    assert unseen_bucket.regime == "IV_Q1_BUFFERED_FALLBACK"

    invalid = model.recommend(
        EntryFeatures(day=make_day(103), atm_iv=None, atm_iv_valid=False), QUANTILE
    )
    assert invalid is None


def test_regime_fallback_forbidden_means_abstention() -> None:
    labels, features = make_two_cluster_dataset()
    config = make_config(min_regime_samples=10, allow_fallback=False)
    model = BufferCalibratedIvModel(config=config).fit(labels, features)

    underfit = model.recommend(
        EntryFeatures(day=make_day(100), atm_iv=0.30, atm_iv_valid=True), QUANTILE
    )
    assert underfit is None
    in_regime = model.recommend(
        EntryFeatures(day=make_day(101), atm_iv=0.10, atm_iv_valid=True), QUANTILE
    )
    assert in_regime is not None
    assert in_regime.regime == "IV_Q3_BUFFERED"


def test_uncalibrated_regime_falls_back_to_unconditional_buffer() -> None:
    """A regime with enough samples but unreachable target uses the fallback.

    Quantile 0.9 keeps the bucket quantile below a single top spike (at 0.95 a
    14-day bucket's quantile IS the max, which would self-cover).
    """

    local_quantile = 0.9
    labels: list[ExcursionLabel] = []
    features: list[EntryFeatures] = []
    # IV_Q3: 13 calm days + 1 spike 115 points above its base quantile, beyond
    # the 100-point grid -> the regime alone is UNCALIBRATED at target 0.99.
    for index in range(14):
        day = make_day(index)
        spiky = index == 13
        labels.append(
            ExcursionLabel(day=day, up_max=120.0 if spiky else 5.0, down_max=5.0)
        )
        features.append(EntryFeatures(day=day, atm_iv=0.10, atm_iv_valid=True))
    for index in range(14, 20):  # IV_Q4: calm wide days
        day = make_day(index)
        labels.append(ExcursionLabel(day=day, up_max=50.0, down_max=50.0))
        features.append(EntryFeatures(day=day, atm_iv=0.30, atm_iv_valid=True))

    config = make_config(quantiles=(local_quantile,), min_regime_samples=10)
    model = BufferCalibratedIvModel(config=config, target_coverage=0.99).fit(
        labels, features
    )
    recommendation = model.recommend(
        EntryFeatures(day=make_day(100), atm_iv=0.10, atm_iv_valid=True), local_quantile
    )
    assert recommendation is not None
    assert recommendation.regime == "IV_Q3_BUFFERED_FALLBACK"


# ---------------------------------------------------------------------------
# Grid edge cases
# ---------------------------------------------------------------------------


def test_empty_training_window_is_rejected() -> None:
    config = make_config()
    with pytest.raises(TailDistancesError, match="must not be empty"):
        BufferCalibratedTailModel(config=config).fit(())
    with pytest.raises(TailDistancesError, match="must not be empty"):
        BufferCalibratedIvModel(config=config).fit((), ())


def test_single_training_day_calibrates_at_zero_buffer() -> None:
    labels = make_labels([(7.0, 9.0)])
    model = BufferCalibratedTailModel(config=make_config()).fit(labels)
    assert model.train_days == 1
    calibration = model.calibration(QUANTILE)
    assert calibration.calibrated is True
    assert (calibration.buffer_up, calibration.buffer_down) == (0.0, 0.0)
    assert calibration.train_coverage == 1.0
    recommendation = model.recommend(
        EntryFeatures(day=make_day(10), atm_iv=None, atm_iv_valid=False), QUANTILE
    )
    assert recommendation is not None
    assert recommendation.up_distance == pytest.approx(7.0)
    assert recommendation.down_distance == pytest.approx(9.0)


def test_unfitted_model_and_quantile_errors() -> None:
    config = make_config()
    model = BufferCalibratedTailModel(config=config)
    feature = EntryFeatures(day=make_day(0), atm_iv=None, atm_iv_valid=False)
    with pytest.raises(TailDistancesError, match="not fitted"):
        model.recommend(feature, QUANTILE)
    fitted = BufferCalibratedTailModel(config=config).fit(make_labels([(5.0, 5.0)]))
    with pytest.raises(TailDistancesError, match="was not fitted"):
        fitted.recommend(feature, 0.99)


# ---------------------------------------------------------------------------
# evaluate_v2 three-way comparison
# ---------------------------------------------------------------------------


def make_stationary_dataset(
    n_days: int, *, seed: int = 7
) -> tuple[list[ExcursionLabel], list[EntryFeatures]]:
    rng = random.Random(seed)
    labels: list[ExcursionLabel] = []
    features: list[EntryFeatures] = []
    for index in range(n_days):
        day = make_day(index)
        labels.append(
            ExcursionLabel(
                day=day,
                up_max=rng.uniform(5.0, 50.0),
                down_max=rng.uniform(5.0, 50.0),
            )
        )
        features.append(
            EntryFeatures(day=day, atm_iv=rng.uniform(0.10, 0.25), atm_iv_valid=True)
        )
    return labels, features


def test_evaluate_v2_returns_three_reports_and_efficiency() -> None:
    labels, features = make_stationary_dataset(80)
    comparison = evaluate_v2(
        labels,
        features,
        config=make_config(min_regime_samples=10),
        quantile=QUANTILE,
        folds=4,
    )
    assert comparison.buffered.model_version == "buffer-calibrated-tail-v2"
    assert comparison.unconditional.model_version == "unconditional-tail-v1"
    assert comparison.iv_regime.model_version == "iv-regime-tail-v1"
    assert len(comparison.efficiency) == 3
    assert [entry.model_version for entry in comparison.efficiency] == [
        "buffer-calibrated-tail-v2",
        "unconditional-tail-v1",
        "iv-regime-tail-v1",
    ]

    buffered = comparison.buffered.aggregate
    unconditional = comparison.unconditional.aggregate
    # Same folds, same base quantiles, non-negative buffers: the buffered
    # corridor weakly dominates the v1 unconditional one on every test day.
    assert buffered.abstain_days == 0
    assert buffered.covered_days >= unconditional.covered_days
    assert buffered.coverage is not None and unconditional.coverage is not None
    assert buffered.coverage >= unconditional.coverage
    assert buffered.mean_up_distance is not None
    assert unconditional.mean_up_distance is not None
    assert buffered.mean_up_distance >= unconditional.mean_up_distance

    for entry in comparison.efficiency:
        assert entry.coverage is not None
        assert entry.mean_total_distance is not None
        assert entry.coverage_per_distance is not None
        assert entry.coverage_per_distance == pytest.approx(
            entry.coverage / entry.mean_total_distance
        )


def test_evaluate_v2_efficiency_is_none_when_fully_abstained() -> None:
    excursions = [
        (5000.0, 5000.0) if index % 30 == 5 else (10.0, 10.0) for index in range(60)
    ]
    comparison = evaluate_v2(
        make_labels(excursions),
        make_features(60),
        config=make_config(min_regime_samples=10),
        quantile=QUANTILE,
        target_coverage=0.99,
        folds=2,
    )
    buffered_efficiency = comparison.efficiency[0]
    assert comparison.buffered.aggregate.evaluated_days == 0
    assert buffered_efficiency.coverage is None
    assert buffered_efficiency.mean_total_distance is None
    assert buffered_efficiency.coverage_per_distance is None
