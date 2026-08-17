from datetime import UTC, datetime, timedelta

import pytest

from marketpilot.domain.events import EventKind, EventRecord, EventSeverity, RiskLockState
from marketpilot.events.risk_lock import RiskLockEngine


def event_fixture(**updates: object) -> EventRecord:
    first_seen = datetime(2026, 8, 17, 12, 30, tzinfo=UTC)
    values: dict[str, object] = {
        "event_id": "cpi-2026-08",
        "kind": EventKind.SCHEDULED,
        "severity": EventSeverity.P0,
        "scheduled_at": first_seen,
        "source_published_at": first_seen,
        "first_seen_at": first_seen,
    }
    values.update(updates)
    return EventRecord.model_validate(values)


def test_unconfirmed_headline_stays_locked_without_directional_interpretation() -> None:
    as_of = datetime(2026, 8, 17, 12, 31, tzinfo=UTC)
    assessment = RiskLockEngine().assess(event_fixture(), as_of)

    assert assessment.state is RiskLockState.LOCKED
    assert "EVENT_NOT_CONFIRMED" in assessment.reasons
    assert "CROSS_ASSET_NOT_CONFIRMED" in assessment.reasons


def test_corroborated_event_waits_for_stability_and_hysteresis() -> None:
    first_seen = datetime(2026, 8, 17, 12, 30, tzinfo=UTC)
    event = event_fixture(
        confirmed_at=first_seen + timedelta(seconds=10),
        corroborating_sources=2,
        cross_asset_confirmed=True,
        cross_asset_confirmed_at=first_seen + timedelta(seconds=10),
        stable_since=first_seen + timedelta(minutes=1),
        stable_observations=3,
    )

    assessment = RiskLockEngine().assess(event, first_seen + timedelta(minutes=2))

    assert assessment.state is RiskLockState.STABILIZING
    assert assessment.rerun_at == first_seen + timedelta(minutes=3)


def test_event_clears_only_after_all_evidence_and_stability_gates() -> None:
    first_seen = datetime(2026, 8, 17, 12, 30, tzinfo=UTC)
    event = event_fixture(
        confirmed_at=first_seen + timedelta(seconds=10),
        corroborating_sources=3,
        cross_asset_confirmed=True,
        cross_asset_confirmed_at=first_seen + timedelta(seconds=10),
        stable_since=first_seen + timedelta(minutes=1),
        stable_observations=4,
    )

    assessment = RiskLockEngine().assess(event, first_seen + timedelta(minutes=3))

    assert assessment.state is RiskLockState.CLEARED
    assert assessment.reasons == ("EVENT_STABLE",)


def test_future_event_is_not_visible_to_replay_clock() -> None:
    with pytest.raises(ValueError, match="not visible"):
        RiskLockEngine().assess(
            event_fixture(),
            datetime(2026, 8, 17, 12, 29, tzinfo=UTC),
        )


def test_checkpoints_include_session_close_and_next_cash_open() -> None:
    anchor = datetime(2026, 8, 17, 12, 30, tzinfo=UTC)
    event = event_fixture(
        session_close_at=datetime(2026, 8, 17, 20, tzinfo=UTC),
        next_cash_open_at=datetime(2026, 8, 18, 13, 30, tzinfo=UTC),
    )

    after_hourly_checks = RiskLockEngine().assess(event, anchor + timedelta(hours=2))
    after_close = RiskLockEngine().assess(
        event,
        datetime(2026, 8, 17, 21, tzinfo=UTC),
    )
    after_next_open = RiskLockEngine().assess(
        event,
        datetime(2026, 8, 18, 14, tzinfo=UTC),
    )

    assert after_hourly_checks.next_checkpoint == "SESSION_CLOSE"
    assert after_close.next_checkpoint == "NEXT_CASH_OPEN"
    assert after_next_open.next_checkpoint == "MONITORING_COMPLETE"


def test_missing_session_boundary_is_explicit_after_hourly_checks() -> None:
    anchor = datetime(2026, 8, 17, 12, 30, tzinfo=UTC)
    assessment = RiskLockEngine().assess(event_fixture(), anchor + timedelta(hours=2))

    assert assessment.next_checkpoint == "SESSION_BOUNDARY_UNCONFIGURED"


def test_next_cash_open_must_follow_session_close() -> None:
    with pytest.raises(ValueError, match="must follow session_close_at"):
        event_fixture(
            session_close_at=datetime(2026, 8, 17, 20, tzinfo=UTC),
            next_cash_open_at=datetime(2026, 8, 17, 13, 30, tzinfo=UTC),
        )


def test_stability_cannot_reuse_pre_confirmation_observations() -> None:
    first_seen = datetime(2026, 8, 17, 12, 30, tzinfo=UTC)
    confirmed = first_seen + timedelta(minutes=2)
    with pytest.raises(ValueError, match="stable_since cannot predate"):
        event_fixture(
            confirmed_at=confirmed,
            corroborating_sources=3,
            cross_asset_confirmed=True,
            cross_asset_confirmed_at=confirmed,
            stable_since=first_seen,
            stable_observations=4,
        )


def test_future_cross_asset_confirmation_does_not_clear_replay() -> None:
    first_seen = datetime(2026, 8, 17, 12, 30, tzinfo=UTC)
    future_confirmation = first_seen + timedelta(minutes=3)
    event = event_fixture(
        confirmed_at=first_seen + timedelta(seconds=10),
        corroborating_sources=3,
        cross_asset_confirmed=True,
        cross_asset_confirmed_at=future_confirmation,
        stable_since=future_confirmation,
        stable_observations=4,
    )

    assessment = RiskLockEngine().assess(event, first_seen + timedelta(minutes=2))

    assert assessment.state is RiskLockState.LOCKED
    assert "CROSS_ASSET_NOT_CONFIRMED" in assessment.reasons
