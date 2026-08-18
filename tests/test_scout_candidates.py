from datetime import UTC, datetime

import pytest

from marketpilot.domain.scout import (
    Candidate,
    CandidateDirection,
    DetectorDescriptor,
    DetectorKind,
    ScoutError,
)

AS_OF = datetime(2026, 8, 18, 14, 0, tzinfo=UTC)
CHECKPOINT = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)


def vol_squeeze_detector() -> DetectorDescriptor:
    return DetectorDescriptor(
        detector_id="scout_vol_squeeze_spx",
        version="0.1.0-draft",
        kind=DetectorKind.VOLATILITY_SQUEEZE,
        universe=("SPX", "SPXW"),
    )


def create_candidate(**overrides: object) -> Candidate:
    kwargs: dict[str, object] = {
        "detector": vol_squeeze_detector(),
        "target": "SPX",
        "direction": CandidateDirection.OPPORTUNITY_LONG_VOLATILITY,
        "as_of": AS_OF,
        "next_checkpoint": CHECKPOINT,
        "confidence": 0.62,
        "evidence": ("IV rank 4th percentile over 1y", "10d realized vol at 6m low"),
        "invalidation_conditions": ("VIX term structure inverts",),
        "data_manifest_id": "sha256:manifest",
    }
    kwargs.update(overrides)
    return Candidate.create(**kwargs)  # type: ignore[arg-type]


def test_valid_candidate_gets_deterministic_frozen_identity() -> None:
    first = create_candidate()
    second = create_candidate()

    assert first.candidate_id == second.candidate_id
    assert first.candidate_id.startswith("sha256:")
    assert first.as_of.tzinfo is UTC


def test_candidate_identity_binds_evidence() -> None:
    baseline = create_candidate()
    altered = create_candidate(evidence=("different evidence",))

    assert altered.candidate_id != baseline.candidate_id


@pytest.mark.parametrize(
    "overrides",
    [
        {"confidence": 1.01},
        {"confidence": -0.1},
        {"evidence": ()},
        {"evidence": ("   ",)},
        {"invalidation_conditions": ()},
        {"data_manifest_id": "  "},
        {"target": "AAPL"},
        {"next_checkpoint": AS_OF},
        {"as_of": datetime(2026, 8, 18, 14, 0)},
    ],
)
def test_contract_violations_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ScoutError):
        create_candidate(**overrides)


def test_gamma_squeeze_requires_estimate_method() -> None:
    detector = DetectorDescriptor(
        detector_id="scout_gamma_squeeze_spx",
        version="0.1.0-draft",
        kind=DetectorKind.GAMMA_SQUEEZE,
        universe=("SPX",),
    )

    with pytest.raises(ScoutError, match="estimate_method"):
        create_candidate(detector=detector)

    candidate = create_candidate(
        detector=detector,
        estimate_method="dealer-gex-estimate-v1",
    )
    assert candidate.estimate_method == "dealer-gex-estimate-v1"


def test_descriptor_requires_declared_universe() -> None:
    with pytest.raises(ScoutError):
        DetectorDescriptor(
            detector_id="scout_iv_crush",
            version="0.1.0-draft",
            kind=DetectorKind.IV_CRUSH,
            universe=(),
        )
