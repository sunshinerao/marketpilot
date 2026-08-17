from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from marketpilot.validation.outcome_labels import (
    OutcomeLabelRequest,
    OutcomeObservation,
    generate_outcome_labels,
)

CUTOFF = datetime(2026, 8, 14, 14, 30, tzinfo=UTC)


def observation(minutes: int, price: float, mtm: float | None = None) -> OutcomeObservation:
    return OutcomeObservation(CUTOFF + timedelta(minutes=minutes), price, mtm)


def request(
    observations: tuple[OutcomeObservation, ...],
) -> OutcomeLabelRequest:
    return OutcomeLabelRequest(
        sample_id="decision-1",
        prediction_cutoff=CUTOFF,
        intraday_end=CUTOFF + timedelta(minutes=60),
        expiry=CUTOFF + timedelta(minutes=120),
        reference_price=6_400.0,
        lower_level=6_380.0,
        upper_level=6_420.0,
        observations=observations,
    )


def test_labels_capture_path_touches_expiry_cross_and_mtm_loss() -> None:
    labels = generate_outcome_labels(
        request(
            (
                observation(10, 6_425.0, -20.0),
                observation(50, 6_375.0, -85.0),
                observation(90, 6_430.0, 15.0),
                observation(120, 6_425.0, 10.0),
            )
        )
    )

    assert labels.maximum_upward_move == 30.0
    assert labels.maximum_downward_move == 25.0
    assert labels.upside_maximum_adverse_move == 25.0
    assert labels.downside_maximum_adverse_move == 30.0
    assert labels.intraday_upper_touch is True
    assert labels.intraday_lower_touch is True
    assert labels.expiry_upper_cross is True
    assert labels.expiry_lower_cross is False
    assert labels.maximum_mtm_loss == 85.0
    assert labels.label_as_of == CUTOFF + timedelta(minutes=120)


@pytest.mark.parametrize("minute", [-1, 0])
def test_labels_reject_any_observation_not_strictly_after_cutoff(minute: int) -> None:
    with pytest.raises(ValueError, match="strictly after prediction_cutoff"):
        request((observation(minute, 6_400.0), observation(120, 6_400.0)))


def test_labels_require_exact_expiry_fact_before_generation() -> None:
    with pytest.raises(ValueError, match="exactly at expiry"):
        request((observation(30, 6_400.0), observation(119, 6_401.0)))


def test_labels_are_time_ordered_and_finite() -> None:
    with pytest.raises(ValueError, match="strictly time ordered"):
        request((observation(30, 6_400.0), observation(20, 6_401.0), observation(120, 6_402.0)))
    with pytest.raises(ValueError, match="finite"):
        observation(20, float("nan"))
