from dataclasses import replace

import pytest

from marketpilot.validation.tail_calibration import (
    TailCalibrationConfig,
    TailCalibrationSample,
    calibrate_joint_tail_corridor,
    empirical_quantile_higher,
    evaluate_tail_coverage,
)


def sample(index: int, up: float, down: float, *, prefix: str = "train") -> TailCalibrationSample:
    return TailCalibrationSample(
        sample_id=f"{prefix}-{index}",
        group_id=f"{prefix}-2026-01-{index + 1:02d}",
        event_type="NORMAL",
        regime="LOW_VOL",
        upward_move=up,
        downward_move=down,
    )


CONFIG = TailCalibrationConfig(
    target_coverage=0.8,
    minimum_samples=5,
    event_type="NORMAL",
    regime="LOW_VOL",
)


def test_higher_quantile_and_joint_artifact_are_deterministic() -> None:
    samples = tuple(sample(index, index + 1, 6 - index) for index in range(5))
    first = calibrate_joint_tail_corridor(
        samples,
        config=CONFIG,
        data_manifest_hash="sha256:data",
    )
    second = calibrate_joint_tail_corridor(
        tuple(reversed(samples)),
        config=CONFIG,
        data_manifest_hash="sha256:data",
    )

    assert empirical_quantile_higher((1, 2, 3, 4, 5), 0.8) == 4
    assert first == second
    assert first.upward_tail >= 4
    assert first.downward_tail >= 5
    first.verify()


def test_insufficient_stratum_and_duplicate_ids_fail_closed() -> None:
    few = tuple(sample(index, 1, 1) for index in range(4))
    with pytest.raises(ValueError, match="insufficient"):
        calibrate_joint_tail_corridor(few, config=CONFIG, data_manifest_hash="sha256:data")
    duplicate = sample(0, 1, 1)
    with pytest.raises(ValueError, match="unique"):
        calibrate_joint_tail_corridor(
            (duplicate,) * 5,
            config=CONFIG,
            data_manifest_hash="sha256:data",
        )


def test_holdout_coverage_is_explicit_and_rejects_overlap() -> None:
    artifact = calibrate_joint_tail_corridor(
        tuple(sample(index, index + 1, index + 1) for index in range(5)),
        config=CONFIG,
        data_manifest_hash="sha256:data",
    )
    report = evaluate_tail_coverage(
        artifact,
        (
            sample(0, 2, 2, prefix="holdout"),
            sample(1, 100, 2, prefix="holdout"),
        ),
    )

    assert report.sample_count == 2
    assert report.downward_coverage == 1
    assert report.upward_coverage == 0.5
    assert report.joint_coverage == 0.5
    with pytest.raises(ValueError, match="overlap"):
        evaluate_tail_coverage(artifact, (sample(0, 1, 1),))
    same_group = replace(
        sample(99, 1, 1, prefix="holdout"),
        group_id="train-2026-01-01",
    )
    with pytest.raises(ValueError, match="groups overlap"):
        evaluate_tail_coverage(artifact, (same_group,))


def test_artifact_and_numeric_inputs_are_tamper_evident_and_finite() -> None:
    artifact = calibrate_joint_tail_corridor(
        tuple(sample(index, index + 1, index + 1) for index in range(5)),
        config=CONFIG,
        data_manifest_hash="sha256:data",
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        replace(artifact, upward_tail=999).verify()
    with pytest.raises(ValueError, match="finite"):
        sample(99, float("nan"), 1)
