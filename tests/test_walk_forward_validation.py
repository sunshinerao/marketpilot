from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from marketpilot.domain.decision import DecisionAction
from marketpilot.validation.metrics import (
    ValidationResult,
    conditional_value_at_risk,
    maximum_drawdown,
    summarize_validation,
)
from marketpilot.validation.walk_forward import PurgedWalkForwardSplitter, ValidationSample

BASE = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def sample(day: int, minute: int, suffix: str) -> ValidationSample:
    return ValidationSample(
        sample_id=f"d{day}-{suffix}",
        observed_at=BASE + timedelta(days=day, minutes=minute),
        group_id=f"trade-date-{day}",
        event_type="P1" if day % 2 else "NONE",
        regime="HIGH_VOL" if day >= 3 else "LOW_VOL",
    )


def test_purged_walk_forward_is_time_ordered_and_keeps_days_together() -> None:
    samples = [sample(day, minute, str(minute)) for day in range(6) for minute in (0, 30)]
    splitter = PurgedWalkForwardSplitter(
        min_train_groups=3,
        test_groups=1,
        purge_gap=timedelta(hours=1),
    )
    folds = splitter.split(tuple(reversed(samples)))

    assert len(folds) == 3
    assert folds[0].test_ids == ("d3-0", "d3-30")
    assert all(sample_id.startswith(("d0-", "d1-", "d2-")) for sample_id in folds[0].train_ids)
    assert max(folds[0].train_ids) not in folds[0].test_ids


def test_purge_gap_removes_training_samples_too_close_to_test() -> None:
    samples = (
        sample(0, 0, "old"),
        sample(1, 23 * 60 + 30, "near"),
        sample(2, 0, "test"),
    )
    fold = PurgedWalkForwardSplitter(
        min_train_groups=2,
        test_groups=1,
        purge_gap=timedelta(hours=1),
    ).split(samples)[0]
    assert fold.train_ids == ("d0-old",)
    assert fold.test_ids == ("d2-test",)


def test_no_trade_effect_and_event_regime_strata_are_explicit() -> None:
    results = (
        ValidationResult(
            sample_id="a",
            event_type="P1",
            regime="HIGH_VOL",
            action=DecisionAction.NO_TRADE,
            counterfactual_pnl=-120.0,
            realized_pnl=None,
            metrics={"coverage": 1.0},
        ),
        ValidationResult(
            sample_id="b",
            event_type="P1",
            regime="HIGH_VOL",
            action=DecisionAction.ENTER,
            counterfactual_pnl=30.0,
            realized_pnl=20.0,
            metrics={"coverage": 0.0},
        ),
        ValidationResult(
            sample_id="c",
            event_type="NONE",
            regime="LOW_VOL",
            action=DecisionAction.ENTER,
            counterfactual_pnl=12.0,
            realized_pnl=10.0,
            metrics={"coverage": 1.0},
        ),
    )
    summaries = summarize_validation(results)
    high_vol = next(summary for summary in summaries if summary.strata[0][1] == "P1")

    assert high_vol.metric_means["coverage"] == pytest.approx(0.5)
    assert high_vol.no_trade_effect.no_trade_count == 1
    assert high_vol.no_trade_effect.filtered_pnl_total == 20.0
    assert high_vol.no_trade_effect.unfiltered_counterfactual_pnl_total == -90.0
    assert high_vol.no_trade_effect.pnl_difference == 110.0


def test_no_trade_result_cannot_claim_realized_pnl() -> None:
    with pytest.raises(ValueError, match="NO_TRADE"):
        ValidationResult(
            sample_id="bad",
            event_type="P0",
            regime="SHOCK",
            action=DecisionAction.NO_TRADE,
            counterfactual_pnl=-10.0,
            realized_pnl=5.0,
        )


def test_cvar_and_drawdown_use_only_explicit_finite_pnl_observations() -> None:
    pnls = (20.0, -10.0, -50.0, 30.0, -5.0)
    assert conditional_value_at_risk(pnls, confidence=0.8) == 50.0
    assert maximum_drawdown(pnls) == 60.0
    with pytest.raises(ValueError, match="finite"):
        conditional_value_at_risk((1.0, float("nan")))
    with pytest.raises(ValueError, match="finite"):
        maximum_drawdown((1.0, float("inf")))


def test_validation_result_rejects_nonfinite_performance() -> None:
    with pytest.raises(ValueError, match="counterfactual_pnl must be finite"):
        ValidationResult(
            sample_id="bad",
            event_type="P0",
            regime="SHOCK",
            action=DecisionAction.NO_TRADE,
            counterfactual_pnl=float("nan"),
            realized_pnl=None,
        )
