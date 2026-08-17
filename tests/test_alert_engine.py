from datetime import UTC, datetime, timedelta

import pytest

from marketpilot.alerts.engine import AlertEngine
from marketpilot.domain.alerts import AlertCandidate, AlertPriority, AlertStatus, FeedbackKind


def candidate(at: datetime, fingerprint: str = "downside-tail") -> AlertCandidate:
    return AlertCandidate(
        fingerprint=fingerprint,
        priority=AlertPriority.P0,
        direction="DOWNSIDE",
        observed_at=at,
        evidence=("VIX_TERM_INVERSION", "ES_LIQUIDITY_DROP"),
        snapshot_id="sha256:demo",
        model_version="0.1.0-baseline",
        rules_version="rules-v1",
        action="RISK_LOCK",
        invalidation_conditions=("CROSS_ASSET_STABLE_2M",),
        rerun_trigger="T+2m",
    )


def test_alert_requires_hysteresis_and_deduplicates_during_cooldown() -> None:
    engine = AlertEngine()
    now = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)

    assert engine.observe(candidate(now)) is None
    created = engine.observe(candidate(now + timedelta(seconds=10)))
    duplicate = engine.observe(candidate(now + timedelta(seconds=20)))

    assert created is not None
    assert duplicate is not None
    assert duplicate.alert_id == created.alert_id
    assert duplicate.deduplicated_count == 1


def test_acknowledgment_is_append_only_feedback_and_stops_escalation() -> None:
    engine = AlertEngine()
    now = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)
    engine.observe(candidate(now))
    alert = engine.observe(candidate(now + timedelta(seconds=10)))
    assert alert is not None

    feedback = engine.record_feedback(
        alert.alert_id,
        FeedbackKind.ACKNOWLEDGED,
        "operator",
        now + timedelta(seconds=30),
    )

    assert feedback in engine.feedback(alert.alert_id)
    assert engine.alerts()[0].status is AlertStatus.ACKNOWLEDGED
    assert engine.due_escalations(now + timedelta(minutes=3)) == ()


def test_unacknowledged_p0_alert_escalates() -> None:
    engine = AlertEngine()
    now = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)
    engine.observe(candidate(now))
    alert = engine.observe(candidate(now + timedelta(seconds=10)))
    assert alert is not None

    due = engine.due_escalations(now + timedelta(minutes=3))

    assert due[0].alert_id == alert.alert_id
    assert due[0].escalation_level == 1


def test_alert_and_feedback_timelines_cannot_move_backward() -> None:
    engine = AlertEngine()
    now = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)
    engine.observe(candidate(now))
    alert = engine.observe(candidate(now + timedelta(seconds=10)))
    assert alert is not None

    with pytest.raises(ValueError, match="observation cannot move backward"):
        engine.observe(candidate(now - timedelta(seconds=1)))
    with pytest.raises(ValueError, match="feedback cannot predate"):
        engine.record_feedback(
            alert.alert_id,
            FeedbackKind.ACKNOWLEDGED,
            "operator",
            now,
        )
