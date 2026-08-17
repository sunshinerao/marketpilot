from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from marketpilot.validation.promotion_gate import (
    PromotionCriteria,
    ValidationSliceEvidence,
    evaluate_promotion_gate,
    load_promotion_criteria,
)

REGISTERED = datetime(2026, 1, 1, tzinfo=UTC)


def criteria() -> PromotionCriteria:
    return PromotionCriteria.create(
        criteria_id="strikepilot-production-v1",
        version="1",
        registered_at=REGISTERED,
        required_slices=(("P0", "HIGH_VOL"), ("NORMAL", "LOW_VOL")),
        minimum_samples_per_slice=100,
        maximum_expiry_cross_rate=0.05,
        maximum_touch_rate=0.10,
        maximum_cvar=500,
        maximum_drawdown=1000,
        minimum_no_trade_pnl_difference=0,
    )


def slice_evidence(event_type: str, regime: str) -> ValidationSliceEvidence:
    return ValidationSliceEvidence(
        event_type=event_type,
        regime=regime,
        sample_count=120,
        expiry_cross_rate=0.04,
        touch_rate=0.09,
        cvar=450,
        maximum_drawdown=900,
        no_trade_pnl_difference=25,
    )


def test_pre_registered_complete_holdout_can_create_a_passing_frozen_report() -> None:
    report = evaluate_promotion_gate(
        criteria(),
        data_manifest_hash="sha256:data",
        holdout_manifest_hash="sha256:holdout",
        holdout_frozen_at=REGISTERED + timedelta(days=1),
        evaluated_at=REGISTERED + timedelta(days=2),
        slices=(slice_evidence("NORMAL", "LOW_VOL"), slice_evidence("P0", "HIGH_VOL")),
    )

    assert report.passed is True
    assert report.failures == ()
    report.verify()


def test_missing_or_failing_slice_never_passes() -> None:
    failed = replace(
        slice_evidence("P0", "HIGH_VOL"),
        sample_count=99,
        expiry_cross_rate=0.06,
        cvar=501,
    )
    report = evaluate_promotion_gate(
        criteria(),
        data_manifest_hash="sha256:data",
        holdout_manifest_hash="sha256:holdout",
        holdout_frozen_at=REGISTERED + timedelta(days=1),
        evaluated_at=REGISTERED + timedelta(days=2),
        slices=(failed,),
    )

    assert report.passed is False
    assert "NORMAL/LOW_VOL:MISSING_SLICE" in report.failures
    assert "P0/HIGH_VOL:INSUFFICIENT_SAMPLES" in report.failures
    assert "P0/HIGH_VOL:EXPIRY_CROSS_EXCEEDED" in report.failures
    assert "P0/HIGH_VOL:CVAR_EXCEEDED" in report.failures


def test_holdout_timeline_and_hashes_are_tamper_evident() -> None:
    registered = criteria()
    with pytest.raises(ValueError, match="after criteria"):
        evaluate_promotion_gate(
            registered,
            data_manifest_hash="sha256:data",
            holdout_manifest_hash="sha256:holdout",
            holdout_frozen_at=REGISTERED,
            evaluated_at=REGISTERED + timedelta(days=2),
            slices=(slice_evidence("P0", "HIGH_VOL"),),
        )
    with pytest.raises(ValueError, match="criteria hash"):
        replace(registered, maximum_cvar=999).verify()


def test_nonfinite_or_duplicate_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        replace(slice_evidence("P0", "HIGH_VOL"), cvar=float("nan"))
    duplicate = slice_evidence("P0", "HIGH_VOL")
    with pytest.raises(ValueError, match="unique"):
        evaluate_promotion_gate(
            criteria(),
            data_manifest_hash="sha256:data",
            holdout_manifest_hash="sha256:holdout",
            holdout_frozen_at=REGISTERED + timedelta(days=1),
            evaluated_at=REGISTERED + timedelta(days=2),
            slices=(duplicate, duplicate),
        )


def test_versioned_repository_criteria_loads_with_a_stable_hash() -> None:
    loaded = load_promotion_criteria("config/promotion-criteria-v1.toml")

    assert loaded.criteria_id == "strikepilot-spxw-shadow-v1"
    assert loaded.required_slices[-1] == ("P0", "HIGH_VOL")
    assert loaded.criteria_hash.startswith("sha256:")
    loaded.verify()
