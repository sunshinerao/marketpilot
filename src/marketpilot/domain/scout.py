from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from marketpilot.domain.snapshot import freeze_snapshot


class ScoutError(ValueError):
    """Raised when a candidate violates the ScoutPilot emission contract."""


class DetectorKind(StrEnum):
    GAMMA_SQUEEZE = "GAMMA_SQUEEZE"
    SHORT_SQUEEZE = "SHORT_SQUEEZE"
    VOLATILITY_SQUEEZE = "VOLATILITY_SQUEEZE"
    IV_CRUSH = "IV_CRUSH"


class CandidateDirection(StrEnum):
    OPPORTUNITY_LONG = "OPPORTUNITY_LONG"
    OPPORTUNITY_LONG_VOLATILITY = "OPPORTUNITY_LONG_VOLATILITY"
    RISK_WARNING = "RISK_WARNING"
    NEUTRAL_WATCH = "NEUTRAL_WATCH"


# Kinds whose core quantity is model-inferred rather than observed. ADR 0003
# requires an explicit estimate method on every such candidate.
_INFERRED_KINDS = frozenset({DetectorKind.GAMMA_SQUEEZE})


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScoutError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DetectorDescriptor:
    """Versioned identity and declared universe of a ScoutPilot detector."""

    detector_id: str
    version: str
    kind: DetectorKind
    universe: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.detector_id.strip():
            raise ScoutError("detector_id must not be blank")
        if not self.version.strip():
            raise ScoutError("version must not be blank")
        if not self.universe or any(not item.strip() for item in self.universe):
            raise ScoutError("universe must declare at least one non-blank target")


@dataclass(frozen=True, slots=True)
class Candidate:
    """An evidence-bounded opportunity or risk observation (ADR 0003).

    A candidate is never a decision: it carries evidence, invalidation
    conditions, and a next checkpoint, and it can only enter the alert pipeline.
    """

    candidate_id: str
    detector_id: str
    detector_version: str
    kind: DetectorKind
    target: str
    direction: CandidateDirection
    as_of: datetime
    next_checkpoint: datetime
    confidence: float
    evidence: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    data_manifest_id: str
    estimate_method: str | None = None

    @classmethod
    def create(
        cls,
        *,
        detector: DetectorDescriptor,
        target: str,
        direction: CandidateDirection,
        as_of: datetime,
        next_checkpoint: datetime,
        confidence: float,
        evidence: tuple[str, ...],
        invalidation_conditions: tuple[str, ...],
        data_manifest_id: str,
        estimate_method: str | None = None,
    ) -> Candidate:
        observed_at = _utc(as_of, "as_of")
        checkpoint = _utc(next_checkpoint, "next_checkpoint")
        if checkpoint <= observed_at:
            raise ScoutError("next_checkpoint must be after as_of")
        if not 0.0 <= confidence <= 1.0:
            raise ScoutError("confidence must be within [0, 1]")
        if not target.strip():
            raise ScoutError("target must not be blank")
        if target.strip() not in detector.universe:
            raise ScoutError(f"target {target!r} is outside the declared detector universe")
        if not evidence or any(not item.strip() for item in evidence):
            raise ScoutError("evidence must contain at least one non-blank entry")
        if not invalidation_conditions or any(
            not item.strip() for item in invalidation_conditions
        ):
            raise ScoutError("invalidation_conditions must contain a non-blank entry")
        if not data_manifest_id.strip():
            raise ScoutError("data_manifest_id must not be blank")
        if detector.kind in _INFERRED_KINDS and not (estimate_method or "").strip():
            raise ScoutError(
                f"{detector.kind.value} candidates must declare an estimate_method "
                "because the underlying quantity is inferred, not observed"
            )

        identity = freeze_snapshot(
            {
                "detector_id": detector.detector_id,
                "detector_version": detector.version,
                "kind": detector.kind,
                "target": target,
                "direction": direction,
                "as_of": observed_at,
                "confidence": confidence,
                "evidence": sorted(evidence),
                "data_manifest_id": data_manifest_id,
                "estimate_method": estimate_method,
            }
        )
        return cls(
            candidate_id=identity.snapshot_id,
            detector_id=detector.detector_id,
            detector_version=detector.version,
            kind=detector.kind,
            target=target.strip(),
            direction=direction,
            as_of=observed_at,
            next_checkpoint=checkpoint,
            confidence=confidence,
            evidence=tuple(evidence),
            invalidation_conditions=tuple(invalidation_conditions),
            data_manifest_id=data_manifest_id,
            estimate_method=estimate_method,
        )


class OpportunityDetector(Protocol):
    """Detector plugin boundary (ADR 0003).

    Implementations read point-in-time inputs and emit zero or more candidates.
    Emitting nothing is a valid, expected outcome for a quiet or uncalibrated
    detector; an uncalibrated detector must not be run at all (promotion gate).
    """

    descriptor: DetectorDescriptor

    def evaluate(self, inputs: Mapping[str, Any]) -> tuple[Candidate, ...]: ...
