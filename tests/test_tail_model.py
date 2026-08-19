"""Hermetic tests for workstream F: tail model v1 + purged walk-forward.

All data is synthetic; no data/raw, no network. The only real file touched is
config/rules-v1.toml, read-only, to prove the quantile grid loads from config.
"""

from __future__ import annotations

import random
from dataclasses import FrozenInstanceError
from datetime import date, timedelta
from pathlib import Path

import pytest

from marketpilot.validation.tail_calibration import empirical_quantile_higher
from marketpilot.validation.tail_distances import TailDistancesError
from marketpilot.validation.tail_model import (
    REGIME_CUTS,
    EntryFeatures,
    EvaluationMetrics,
    EvaluationReport,
    ExcursionLabel,
    IvRegimeTailModel,
    TailModelConfig,
    UnconditionalTailModel,
    evaluate_against_unconditional_baseline,
    load_tail_model_config,
    walk_forward_evaluate,
)

RULES_PATH = Path(__file__).resolve().parents[1] / "config" / "rules-v1.toml"
DAY_ZERO = date(2026, 1, 5)


def make_day(index: int) -> date:
    return DAY_ZERO + timedelta(days=index)


def make_regime_dataset(
    n_days: int, *, seed: int = 11
) -> tuple[list[ExcursionLabel], list[EntryFeatures]]:
    """Low-IV days get narrow excursions, high-IV days wide ones."""

    rng = random.Random(seed)
    labels: list[ExcursionLabel] = []
    features: list[EntryFeatures] = []
    for index in range(n_days):
        if index % 5 == 4:  # 20% high-IV days
            atm_iv = rng.uniform(0.22, 0.28)
            up_max = rng.uniform(40.0, 80.0)
            down_max = rng.uniform(40.0, 80.0)
        else:
            atm_iv = rng.uniform(0.10, 0.13)
            up_max = rng.uniform(4.0, 10.0)
            down_max = rng.uniform(4.0, 10.0)
        day = make_day(index)
        labels.append(
            ExcursionLabel(day=day, up_max=up_max, down_max=down_max, entry_price=6000.0)
        )
        features.append(EntryFeatures(day=day, atm_iv=atm_iv, atm_iv_valid=True))
    return labels, features


def mean_distance(metrics: EvaluationMetrics) -> float:
    assert metrics.mean_up_distance is not None
    assert metrics.mean_down_distance is not None
    return (metrics.mean_up_distance + metrics.mean_down_distance) / 2


def efficiency(metrics: EvaluationMetrics) -> float:
    """Coverage achieved per point of recommended corridor width."""

    assert metrics.coverage is not None
    return metrics.coverage / mean_distance(metrics)


# ---------------------------------------------------------------------------
# Config loading (real rules file, read-only)
# ---------------------------------------------------------------------------


def test_quantile_config_loads_from_real_rules_file() -> None:
    config = load_tail_model_config(RULES_PATH)
    assert config.quantiles == (0.95, 0.975, 0.99)
    assert config.min_regime_samples == 20
    assert config.allow_fallback is True


def test_quantile_config_rejects_rules_without_risk_section(tmp_path: Path) -> None:
    rules = tmp_path / "rules.toml"
    rules.write_text('version = "x"\n', encoding="utf-8")
    with pytest.raises(TailDistancesError, match="risk quantiles"):
        load_tail_model_config(rules)


def test_tail_model_config_validates_quantiles() -> None:
    with pytest.raises(TailDistancesError, match="must not be empty"):
        TailModelConfig(quantiles=())
    with pytest.raises(TailDistancesError, match="within \\(0, 1\\)"):
        TailModelConfig(quantiles=(0.95, 1.5))
    with pytest.raises(TailDistancesError, match="unique"):
        TailModelConfig(quantiles=(0.95, 0.95))
    with pytest.raises(TailDistancesError, match="min_regime_samples"):
        TailModelConfig(quantiles=(0.95,), min_regime_samples=0)


# ---------------------------------------------------------------------------
# Unconditional baseline
# ---------------------------------------------------------------------------


def test_unconditional_model_fits_empirical_quantiles() -> None:
    labels = [
        ExcursionLabel(
            day=make_day(index),
            up_max=float(index + 1),
            down_max=float(2 * (index + 1)),
        )
        for index in range(100)
    ]
    config = TailModelConfig(quantiles=(0.95, 0.99))
    model = UnconditionalTailModel(config=config).fit(labels, ())

    assert model.train_days == 100
    # The baseline needs no features: even an invalid feature row is served.
    recommendation = model.recommend(
        EntryFeatures(day=make_day(200), atm_iv=None, atm_iv_valid=False), 0.95
    )
    assert recommendation is not None
    assert recommendation.regime == "ALL"
    assert recommendation.model_version == "unconditional-tail-v1"
    assert recommendation.quantile == 0.95
    assert recommendation.up_distance == empirical_quantile_higher(
        tuple(label.up_max for label in labels), 0.95
    )
    assert recommendation.down_distance == empirical_quantile_higher(
        tuple(label.down_max for label in labels), 0.95
    )


def test_unconditional_model_rejects_unfitted_or_unknown_quantile() -> None:
    config = TailModelConfig(quantiles=(0.95,))
    model = UnconditionalTailModel(config=config)
    feature = EntryFeatures(day=make_day(0), atm_iv=0.12, atm_iv_valid=True)
    with pytest.raises(TailDistancesError, match="not fitted"):
        model.recommend(feature, 0.95)
    fitted = model.fit([ExcursionLabel(day=make_day(0), up_max=1.0, down_max=1.0)], ())
    with pytest.raises(TailDistancesError, match="was not fitted"):
        fitted.recommend(feature, 0.5)
    with pytest.raises(TailDistancesError, match="must not be empty"):
        model.fit([], ())


def test_zero_excursion_quantiles_are_floored_to_positive_distances() -> None:
    labels = [ExcursionLabel(day=make_day(i), up_max=0.0, down_max=0.0) for i in range(30)]
    model = UnconditionalTailModel(config=TailModelConfig(quantiles=(0.95,))).fit(labels, ())
    recommendation = model.recommend(
        EntryFeatures(day=make_day(30), atm_iv=0.1, atm_iv_valid=True), 0.95
    )
    assert recommendation is not None
    assert recommendation.up_distance > 0
    assert recommendation.down_distance > 0


# ---------------------------------------------------------------------------
# IV-regime conditional model
# ---------------------------------------------------------------------------


def test_regime_boundaries_come_from_training_window_only() -> None:
    rng = random.Random(3)
    config = TailModelConfig(quantiles=(0.95,), min_regime_samples=2)
    train_features = [
        EntryFeatures(
            day=make_day(i), atm_iv=rng.uniform(0.10, 0.20), atm_iv_valid=True
        )
        for i in range(100)
    ]
    train_labels = [
        ExcursionLabel(day=feature.day, up_max=10.0, down_max=10.0)
        for feature in train_features
    ]
    model = IvRegimeTailModel(config=config).fit(train_labels, train_features)

    train_ivs = tuple(
        feature.atm_iv for feature in train_features if feature.atm_iv is not None
    )
    expected = tuple(empirical_quantile_higher(train_ivs, cut) for cut in REGIME_CUTS)
    assert model.regime_boundaries == expected

    # A test window of extreme IV would move the boundaries if it leaked in.
    test_features = [
        EntryFeatures(
            day=make_day(100 + i), atm_iv=rng.uniform(0.50, 0.60), atm_iv_valid=True
        )
        for i in range(30)
    ]
    test_labels = [
        ExcursionLabel(day=feature.day, up_max=50.0, down_max=50.0)
        for feature in test_features
    ]
    leaked = IvRegimeTailModel(config=config).fit(
        train_labels + test_labels, train_features + test_features
    )
    assert leaked.regime_boundaries != model.regime_boundaries

    # The frozen model keeps its training boundaries while scoring test days.
    recommendation = model.recommend(test_features[0], 0.95)
    assert recommendation is not None
    assert recommendation.regime == "IV_Q4"  # far above the top train boundary
    assert model.regime_boundaries == expected


def test_regime_assignment_respects_boundaries() -> None:
    config = TailModelConfig(quantiles=(0.95,), min_regime_samples=1)
    features = [
        EntryFeatures(day=make_day(i), atm_iv=iv, atm_iv_valid=True)
        for i, iv in enumerate((0.05, 0.10, 0.20, 0.30, 0.40))
    ]
    labels = [
        ExcursionLabel(day=feature.day, up_max=5.0, down_max=5.0) for feature in features
    ]
    model = IvRegimeTailModel(config=config).fit(labels, features)
    probe_day = make_day(99)
    cases = (
        (0.05, "IV_Q1"),
        (0.20, "IV_Q3"),  # bisect_right: ties land in the upper bucket
        (0.25, "IV_Q3"),
        (0.35, "IV_Q4"),
    )
    for atm_iv, expected_regime in cases:
        recommendation = model.recommend(
            EntryFeatures(day=probe_day, atm_iv=atm_iv, atm_iv_valid=True), 0.95
        )
        assert recommendation is not None
        assert recommendation.regime == expected_regime


def test_sparse_regime_falls_back_to_unconditional_quantiles() -> None:
    # 80 days of uniform IV -> every quartile has 20 days < min_regime_samples.
    rng = random.Random(5)
    config = TailModelConfig(quantiles=(0.95,), min_regime_samples=25)
    features = [
        EntryFeatures(
            day=make_day(i), atm_iv=rng.uniform(0.10, 0.20), atm_iv_valid=True
        )
        for i in range(80)
    ]
    labels = [
        ExcursionLabel(
            day=feature.day,
            up_max=rng.uniform(5.0, 15.0),
            down_max=rng.uniform(5.0, 15.0),
        )
        for feature in features
    ]
    model = IvRegimeTailModel(config=config).fit(labels, features)

    assert all(count < 25 for count in model.regime_sample_counts.values())
    recommendation = model.recommend(features[0], 0.95)
    assert recommendation is not None
    assert recommendation.regime.endswith("_FALLBACK")
    assert recommendation.up_distance == empirical_quantile_higher(
        tuple(label.up_max for label in labels), 0.95
    )
    assert recommendation.down_distance == empirical_quantile_higher(
        tuple(label.down_max for label in labels), 0.95
    )


def test_fallback_forbidden_turns_unfit_regime_into_no_trade() -> None:
    rng = random.Random(6)
    config = TailModelConfig(
        quantiles=(0.95,), min_regime_samples=25, allow_fallback=False
    )
    features = [
        EntryFeatures(
            day=make_day(i), atm_iv=rng.uniform(0.10, 0.20), atm_iv_valid=True
        )
        for i in range(80)
    ]
    labels = [
        ExcursionLabel(day=feature.day, up_max=10.0, down_max=10.0)
        for feature in features
    ]
    model = IvRegimeTailModel(config=config).fit(labels, features)
    assert model.recommend(features[0], 0.95) is None


def test_invalid_features_force_abstention() -> None:
    config = TailModelConfig(quantiles=(0.95,), min_regime_samples=2)
    features = [
        EntryFeatures(
            day=make_day(i), atm_iv=0.10 + 0.01 * (i % 4), atm_iv_valid=True
        )
        for i in range(40)
    ]
    labels = [
        ExcursionLabel(day=feature.day, up_max=10.0, down_max=10.0)
        for feature in features
    ]
    model = IvRegimeTailModel(config=config).fit(labels, features)
    probe = make_day(99)
    assert (
        model.recommend(EntryFeatures(day=probe, atm_iv=0.12, atm_iv_valid=False), 0.95)
        is None
    )
    assert (
        model.recommend(EntryFeatures(day=probe, atm_iv=None, atm_iv_valid=True), 0.95)
        is None
    )


def test_conditional_model_rejects_unfitted_or_invalid_training_data() -> None:
    config = TailModelConfig(quantiles=(0.95,), min_regime_samples=2)
    model = IvRegimeTailModel(config=config)
    probe = EntryFeatures(day=make_day(0), atm_iv=0.12, atm_iv_valid=True)
    with pytest.raises(TailDistancesError, match="not fitted"):
        model.recommend(probe, 0.95)
    with pytest.raises(TailDistancesError, match="must not be empty"):
        model.fit([], [])
    labels = [ExcursionLabel(day=make_day(i), up_max=1.0, down_max=1.0) for i in range(5)]
    invalid = [
        EntryFeatures(day=make_day(i), atm_iv=None, atm_iv_valid=False) for i in range(5)
    ]
    with pytest.raises(TailDistancesError, match="valid atm_iv"):
        model.fit(labels, invalid)


# ---------------------------------------------------------------------------
# Purged walk-forward harness
# ---------------------------------------------------------------------------


def test_walk_forward_conditional_beats_unconditional_efficiency() -> None:
    labels, features = make_regime_dataset(240)
    config = load_tail_model_config(RULES_PATH)
    comparison = evaluate_against_unconditional_baseline(
        labels, features, config=config, quantile=0.95, folds=4
    )
    conditional = comparison.conditional
    baseline = comparison.baseline

    assert len(conditional.folds) == 4
    assert len(baseline.folds) == 4
    assert conditional.model_version == "iv-regime-tail-v1"
    assert baseline.model_version == "unconditional-tail-v1"
    assert conditional.aggregate.abstain_days == 0
    assert baseline.aggregate.abstain_days == 0
    assert conditional.aggregate.coverage is not None
    assert baseline.aggregate.coverage is not None

    # Comparable coverage at a much tighter corridor -> strictly better efficiency.
    assert conditional.aggregate.coverage >= baseline.aggregate.coverage - 0.10
    assert mean_distance(conditional.aggregate) < mean_distance(baseline.aggregate)
    assert efficiency(conditional.aggregate) > 1.25 * efficiency(baseline.aggregate)


def test_walk_forward_purges_and_embargoes_every_fold() -> None:
    labels, features = make_regime_dataset(120)
    config = TailModelConfig(quantiles=(0.95,), min_regime_samples=5)
    purge_gap = timedelta(days=1)
    report = walk_forward_evaluate(
        labels,
        features,
        model_factory=lambda: UnconditionalTailModel(config=config),
        quantile=0.95,
        folds=3,
        purge_gap=purge_gap,
    )
    assert len(report.folds) == 3
    assert report.purge_gap == purge_gap
    previous_test_end = None
    for fold in report.folds:
        assert fold.purge_cutoff == fold.test_start - purge_gap
        assert fold.purge_cutoff < fold.test_start
        assert fold.train_days >= 1
        if previous_test_end is not None:
            assert fold.test_start > previous_test_end
        previous_test_end = fold.test_end


def test_walk_forward_counts_per_side_breaches() -> None:
    # One test-window day spikes up only; down side is always covered.
    labels = [
        ExcursionLabel(
            day=make_day(i),
            up_max=50.0 if i == 45 else 5.0,
            down_max=1.0,
        )
        for i in range(60)
    ]
    features = [
        EntryFeatures(day=make_day(i), atm_iv=0.12, atm_iv_valid=True) for i in range(60)
    ]
    config = TailModelConfig(quantiles=(0.95,))
    report = walk_forward_evaluate(
        labels,
        features,
        model_factory=lambda: UnconditionalTailModel(config=config),
        quantile=0.95,
        folds=3,
    )
    aggregate = report.aggregate
    assert aggregate.test_days == 30
    assert aggregate.up_breach_count == 1
    assert aggregate.down_breach_count == 0
    assert aggregate.covered_days == 29
    assert aggregate.coverage == pytest.approx(29 / 30)
    assert aggregate.mean_up_distance == pytest.approx(5.0)
    assert aggregate.median_down_distance == pytest.approx(1.0)


def test_abstain_days_are_counted_and_excluded_from_coverage() -> None:
    labels = [
        ExcursionLabel(day=make_day(i), up_max=1.0, down_max=1.0) for i in range(60)
    ]
    features = [
        EntryFeatures(
            day=make_day(i),
            atm_iv=None if 45 <= i <= 49 else 0.10 + 0.002 * (i % 7),
            atm_iv_valid=not (45 <= i <= 49),
        )
        for i in range(60)
    ]
    config = TailModelConfig(quantiles=(0.95,), min_regime_samples=5)
    report = walk_forward_evaluate(
        labels,
        features,
        model_factory=lambda: IvRegimeTailModel(config=config),
        quantile=0.95,
        folds=3,
    )
    aggregate = report.aggregate
    assert aggregate.abstain_days == 5
    assert aggregate.evaluated_days == 25
    assert aggregate.no_trade_share == pytest.approx(5 / 30)
    # All evaluated days are covered; abstain days are out of the denominator.
    assert aggregate.coverage == pytest.approx(1.0)
    fold_with_abstentions = report.folds[1]
    assert fold_with_abstentions.metrics.abstain_days == 5
    assert fold_with_abstentions.metrics.no_trade_share == pytest.approx(5 / 10)


def test_walk_forward_accepts_raw_jsonl_style_records() -> None:
    labels = [
        {
            "day": make_day(i).isoformat(),
            "up_max": 5.0 + (i % 7),
            "down_max": 4.0 + (i % 5),
            "entry_price": 6010.5,
            "ignored_extra_field": "x",
        }
        for i in range(60)
    ]
    features = [
        {
            "day": make_day(i).isoformat(),
            "atm_iv": 0.12 + 0.001 * (i % 9),
            "skew": -0.1,
            "realized_vol_30m": 0.008,
            "atm_iv_valid": True,
        }
        for i in range(60)
    ]
    config = TailModelConfig(quantiles=(0.95,), min_regime_samples=5)
    report = walk_forward_evaluate(
        labels,
        features,
        model_factory=lambda: IvRegimeTailModel(config=config),
        quantile=0.95,
        folds=3,
    )
    assert report.aggregate.test_days == 30
    assert report.aggregate.abstain_days == 0


def test_walk_forward_rejects_bad_inputs() -> None:
    labels = [ExcursionLabel(day=make_day(i), up_max=1.0, down_max=1.0) for i in range(10)]
    features = [
        EntryFeatures(day=make_day(i), atm_iv=0.1, atm_iv_valid=True) for i in range(10)
    ]
    config = TailModelConfig(quantiles=(0.95,))

    def factory() -> UnconditionalTailModel:
        return UnconditionalTailModel(config=config)
    with pytest.raises(ValueError, match="quantile"):
        walk_forward_evaluate(labels, features, model_factory=factory, quantile=1.5, folds=2)
    with pytest.raises(ValueError, match="folds must be positive"):
        walk_forward_evaluate(labels, features, model_factory=factory, quantile=0.95, folds=0)
    with pytest.raises(ValueError, match="purge_gap"):
        walk_forward_evaluate(
            labels,
            features,
            model_factory=factory,
            quantile=0.95,
            folds=2,
            purge_gap=timedelta(days=-1),
        )
    with pytest.raises(ValueError, match="duplicate excursion label"):
        walk_forward_evaluate(
            labels + [labels[0]], features, model_factory=factory, quantile=0.95, folds=2
        )
    with pytest.raises(ValueError, match="duplicate entry features"):
        walk_forward_evaluate(
            labels, features + [features[0]], model_factory=factory, quantile=0.95, folds=2
        )
    with pytest.raises(ValueError, match="must not be empty"):
        walk_forward_evaluate([], features, model_factory=factory, quantile=0.95, folds=2)
    with pytest.raises(ValueError, match="min_train_days"):
        walk_forward_evaluate(
            labels,
            features,
            model_factory=factory,
            quantile=0.95,
            folds=2,
            min_train_days=10,
        )


def test_report_dataclasses_are_frozen() -> None:
    labels, features = make_regime_dataset(60)
    config = TailModelConfig(quantiles=(0.95,), min_regime_samples=5)
    report = walk_forward_evaluate(
        labels,
        features,
        model_factory=lambda: IvRegimeTailModel(config=config),
        quantile=0.95,
        folds=3,
    )
    with pytest.raises(FrozenInstanceError):
        report.quantile = 0.5  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        report.aggregate.test_days = 0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        report.folds[0].train_days = 0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Record parsing validation
# ---------------------------------------------------------------------------


def test_record_parsing_rejects_bad_shapes() -> None:
    with pytest.raises(TailDistancesError, match="missing field"):
        ExcursionLabel.from_mapping({"day": "2026-01-05", "up_max": 1.0})
    with pytest.raises(TailDistancesError, match="ISO date"):
        ExcursionLabel.from_mapping({"day": "not-a-date", "up_max": 1.0, "down_max": 1.0})
    with pytest.raises(TailDistancesError, match="finite and non-negative"):
        ExcursionLabel.from_mapping(
            {"day": "2026-01-05", "up_max": -1.0, "down_max": 1.0}
        )
    with pytest.raises(TailDistancesError, match="entry_price must be positive"):
        ExcursionLabel(day=make_day(0), up_max=1.0, down_max=1.0, entry_price=0.0)
    with pytest.raises(TailDistancesError, match="missing field"):
        EntryFeatures.from_mapping({"atm_iv": 0.12})
    with pytest.raises(TailDistancesError, match="atm_iv_valid must be a boolean"):
        EntryFeatures.from_mapping({"day": "2026-01-05", "atm_iv": 0.12, "atm_iv_valid": 1})
    with pytest.raises(TailDistancesError, match="finite and non-negative"):
        EntryFeatures(day=make_day(0), atm_iv=-0.1, atm_iv_valid=True)
    with pytest.raises(TailDistancesError, match="unsupported label record"):
        walk_forward_evaluate(
            [42], [], model_factory=lambda: None, quantile=0.95, folds=1  # type: ignore[list-item, arg-type]
        )
    with pytest.raises(TailDistancesError, match="unsupported feature record"):
        walk_forward_evaluate(
            [ExcursionLabel(day=make_day(0), up_max=1.0, down_max=1.0)],
            [42],  # type: ignore[list-item]
            model_factory=lambda: None,  # type: ignore[arg-type]
            quantile=0.95,
            folds=1,
        )


def test_entry_features_default_validity_from_atm_iv_presence() -> None:
    with_iv = EntryFeatures.from_mapping({"day": "2026-01-05", "atm_iv": 0.12})
    assert with_iv.atm_iv_valid is True
    without_iv = EntryFeatures.from_mapping({"day": "2026-01-05", "atm_iv": None})
    assert without_iv.atm_iv_valid is False


# ---------------------------------------------------------------------------
# Metrics/report contract validation
# ---------------------------------------------------------------------------


def test_evaluation_metrics_rejects_inconsistent_counts() -> None:
    base = {
        "test_days": 10,
        "abstain_days": 2,
        "evaluated_days": 8,
        "covered_days": 7,
        "up_breach_count": 1,
        "down_breach_count": 1,
        "coverage": 7 / 8,
        "no_trade_share": 0.2,
        "mean_up_distance": 12.0,
        "mean_down_distance": 11.0,
        "median_up_distance": 12.0,
        "median_down_distance": 11.0,
    }
    assert EvaluationMetrics(**base).covered_days == 7
    with pytest.raises(ValueError, match="add up"):
        EvaluationMetrics(**{**base, "evaluated_days": 7})
    with pytest.raises(ValueError, match="cannot exceed"):
        EvaluationMetrics(**{**base, "covered_days": 9})
    with pytest.raises(ValueError, match="undefined without evaluated days"):
        EvaluationMetrics(
            **{
                **base,
                "test_days": 2,
                "abstain_days": 2,
                "evaluated_days": 0,
                "covered_days": 0,
                "up_breach_count": 0,
                "down_breach_count": 0,
                "no_trade_share": 1.0,
                "mean_up_distance": None,
                "mean_down_distance": None,
                "median_up_distance": None,
                "median_down_distance": None,
            }
        )


def test_evaluation_report_requires_folds() -> None:
    metrics = EvaluationMetrics(
        test_days=1,
        abstain_days=1,
        evaluated_days=0,
        covered_days=0,
        up_breach_count=0,
        down_breach_count=0,
        coverage=None,
        no_trade_share=1.0,
        mean_up_distance=None,
        mean_down_distance=None,
        median_up_distance=None,
        median_down_distance=None,
    )
    assert metrics.coverage is None
    with pytest.raises(ValueError, match="at least one fold"):
        EvaluationReport(
            model_version="iv-regime-tail-v1",
            quantile=0.95,
            purge_gap=timedelta(days=1),
            folds=(),
            aggregate=metrics,
        )
