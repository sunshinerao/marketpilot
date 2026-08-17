from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace

from marketpilot.domain.snapshot import freeze_snapshot


def _finite_nonnegative(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return result


def empirical_quantile_higher(values: tuple[float, ...], quantile: float) -> float:
    """Deterministic conservative empirical quantile without interpolation."""

    if not values:
        raise ValueError("quantile values must not be empty")
    if not 0 < quantile < 1:
        raise ValueError("quantile must be in (0, 1)")
    ordered = sorted(_finite_nonnegative(value, "quantile value") for value in values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


@dataclass(frozen=True, slots=True)
class TailCalibrationSample:
    sample_id: str
    group_id: str
    event_type: str
    regime: str
    upward_move: float
    downward_move: float

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.sample_id, self.group_id, self.event_type, self.regime)
        ):
            raise ValueError("sample identity and strata must not be blank")
        object.__setattr__(
            self,
            "upward_move",
            _finite_nonnegative(self.upward_move, "upward_move"),
        )
        object.__setattr__(
            self,
            "downward_move",
            _finite_nonnegative(self.downward_move, "downward_move"),
        )


@dataclass(frozen=True, slots=True)
class TailCalibrationConfig:
    target_coverage: float
    minimum_samples: int
    event_type: str
    regime: str

    def __post_init__(self) -> None:
        if not 0.5 < self.target_coverage < 1:
            raise ValueError("target_coverage must be in (0.5, 1)")
        if self.minimum_samples < 2:
            raise ValueError("minimum_samples must be at least 2")
        if not self.event_type.strip() or not self.regime.strip():
            raise ValueError("event_type and regime are required")


@dataclass(frozen=True, slots=True)
class TailCalibrationArtifact:
    artifact_version: str
    data_manifest_hash: str
    config: TailCalibrationConfig
    sample_ids: tuple[str, ...]
    group_ids: tuple[str, ...]
    upward_tail: float
    downward_tail: float
    joint_buffer: float
    artifact_hash: str

    def verify(self) -> None:
        payload = asdict(self)
        payload.pop("artifact_hash")
        if self.artifact_hash != freeze_snapshot(payload).snapshot_id:
            raise ValueError("tail calibration artifact hash mismatch")


@dataclass(frozen=True, slots=True)
class TailCoverageReport:
    sample_count: int
    upward_coverage: float
    downward_coverage: float
    joint_coverage: float


def calibrate_joint_tail_corridor(
    samples: tuple[TailCalibrationSample, ...],
    *,
    config: TailCalibrationConfig,
    data_manifest_hash: str,
    artifact_version: str = "joint-tail-v1",
) -> TailCalibrationArtifact:
    """Fit marginal higher-quantiles plus an additive joint conformal buffer."""

    if not data_manifest_hash.strip() or not artifact_version.strip():
        raise ValueError("data manifest hash and artifact version are required")
    if len({sample.sample_id for sample in samples}) != len(samples):
        raise ValueError("sample_id values must be unique")
    filtered = tuple(
        sample
        for sample in samples
        if sample.event_type == config.event_type and sample.regime == config.regime
    )
    if len(filtered) < config.minimum_samples:
        raise ValueError("insufficient stratum samples; tail corridor must remain unavailable")

    upward_base = empirical_quantile_higher(
        tuple(sample.upward_move for sample in filtered),
        config.target_coverage,
    )
    downward_base = empirical_quantile_higher(
        tuple(sample.downward_move for sample in filtered),
        config.target_coverage,
    )
    nonconformity = tuple(
        max(
            0.0,
            sample.upward_move - upward_base,
            sample.downward_move - downward_base,
        )
        for sample in filtered
    )
    finite_sample_quantile = min(
        1 - 1e-12,
        math.ceil((len(filtered) + 1) * config.target_coverage) / len(filtered),
    )
    joint_buffer = empirical_quantile_higher(nonconformity, finite_sample_quantile)
    candidate = TailCalibrationArtifact(
        artifact_version=artifact_version,
        data_manifest_hash=data_manifest_hash,
        config=config,
        sample_ids=tuple(sorted(sample.sample_id for sample in filtered)),
        group_ids=tuple(sorted({sample.group_id for sample in filtered})),
        upward_tail=upward_base + joint_buffer,
        downward_tail=downward_base + joint_buffer,
        joint_buffer=joint_buffer,
        artifact_hash="",
    )
    payload = asdict(candidate)
    payload.pop("artifact_hash")
    artifact = replace(candidate, artifact_hash=freeze_snapshot(payload).snapshot_id)
    artifact.verify()
    return artifact


def evaluate_tail_coverage(
    artifact: TailCalibrationArtifact,
    holdout: tuple[TailCalibrationSample, ...],
) -> TailCoverageReport:
    """Report marginal and simultaneous corridor coverage on an untouched holdout."""

    artifact.verify()
    relevant = tuple(
        sample
        for sample in holdout
        if sample.event_type == artifact.config.event_type
        and sample.regime == artifact.config.regime
    )
    if not relevant:
        raise ValueError("holdout has no samples for the calibrated stratum")
    if set(artifact.sample_ids).intersection(sample.sample_id for sample in relevant):
        raise ValueError("calibration and holdout sample ids overlap")
    if set(artifact.group_ids).intersection(sample.group_id for sample in relevant):
        raise ValueError("calibration and holdout groups overlap")
    upward = sum(sample.upward_move <= artifact.upward_tail for sample in relevant)
    downward = sum(sample.downward_move <= artifact.downward_tail for sample in relevant)
    joint = sum(
        sample.upward_move <= artifact.upward_tail
        and sample.downward_move <= artifact.downward_tail
        for sample in relevant
    )
    count = len(relevant)
    return TailCoverageReport(
        sample_count=count,
        upward_coverage=upward / count,
        downward_coverage=downward / count,
        joint_coverage=joint / count,
    )
