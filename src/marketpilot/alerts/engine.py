from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from marketpilot.domain.alerts import (
    AlertCandidate,
    AlertFeedback,
    AlertPriority,
    AlertRecord,
    AlertStatus,
    FeedbackKind,
)


@dataclass(frozen=True, slots=True)
class AlertPolicy:
    hysteresis_observations: int = 2
    cooldown: timedelta = timedelta(minutes=5)
    p0_escalation_after: timedelta = timedelta(minutes=2)


class AlertEngine:
    """In-process alert state machine; all user actions remain append-only evidence."""

    def __init__(self, policy: AlertPolicy | None = None) -> None:
        self._policy = policy or AlertPolicy()
        self._observations: dict[str, int] = {}
        self._alerts: dict[str, AlertRecord] = {}
        self._latest_by_fingerprint: dict[str, str] = {}
        self._feedback: list[AlertFeedback] = []

    def observe(self, candidate: AlertCandidate, active: bool = True) -> AlertRecord | None:
        fingerprint = candidate.fingerprint
        if not active:
            self._observations.pop(fingerprint, None)
            return None
        count = self._observations.get(fingerprint, 0) + 1
        self._observations[fingerprint] = count
        if count < self._policy.hysteresis_observations:
            return None

        previous = self._latest(fingerprint)
        if previous is not None:
            elapsed = candidate.observed_at - previous.created_at
            if elapsed < timedelta(0):
                raise ValueError("alert observation cannot move backward in time")
            if elapsed < self._policy.cooldown:
                updated = previous.model_copy(
                    update={"deduplicated_count": previous.deduplicated_count + 1}
                )
                self._alerts[updated.alert_id] = updated
                return updated

        record = AlertRecord(
            alert_id=str(uuid4()),
            candidate=candidate,
            created_at=candidate.observed_at.astimezone(UTC),
        )
        self._alerts[record.alert_id] = record
        self._latest_by_fingerprint[fingerprint] = record.alert_id
        return record

    def record_feedback(
        self,
        alert_id: str,
        kind: FeedbackKind,
        actor: str,
        recorded_at: datetime,
        note: str | None = None,
    ) -> AlertFeedback:
        if alert_id not in self._alerts:
            raise KeyError(f"unknown alert: {alert_id}")
        if recorded_at < self._alerts[alert_id].created_at:
            raise ValueError("feedback cannot predate the alert")
        feedback = AlertFeedback(
            feedback_id=str(uuid4()),
            alert_id=alert_id,
            kind=kind,
            recorded_at=recorded_at,
            actor=actor,
            note=note,
        )
        self._feedback.append(feedback)
        status = self._status_for(kind)
        if status is not None:
            current = self._alerts[alert_id]
            self._alerts[alert_id] = current.model_copy(update={"status": status})
        return feedback

    def due_escalations(self, as_of: datetime) -> tuple[AlertRecord, ...]:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        due: list[AlertRecord] = []
        for alert_id, alert in tuple(self._alerts.items()):
            if (
                alert.candidate.priority is AlertPriority.P0
                and alert.status is AlertStatus.OPEN
                and as_of - alert.created_at >= self._policy.p0_escalation_after
            ):
                escalated = alert.model_copy(
                    update={"escalation_level": alert.escalation_level + 1}
                )
                self._alerts[alert_id] = escalated
                due.append(escalated)
        return tuple(due)

    def alerts(self) -> tuple[AlertRecord, ...]:
        return tuple(sorted(self._alerts.values(), key=lambda item: item.created_at, reverse=True))

    def feedback(self, alert_id: str | None = None) -> tuple[AlertFeedback, ...]:
        return tuple(
            item for item in self._feedback if alert_id is None or item.alert_id == alert_id
        )

    def _latest(self, fingerprint: str) -> AlertRecord | None:
        alert_id = self._latest_by_fingerprint.get(fingerprint)
        return self._alerts.get(alert_id) if alert_id is not None else None

    @staticmethod
    def _status_for(kind: FeedbackKind) -> AlertStatus | None:
        if kind is FeedbackKind.ACKNOWLEDGED:
            return AlertStatus.ACKNOWLEDGED
        if kind in {FeedbackKind.DISMISSED, FeedbackKind.FALSE_POSITIVE}:
            return AlertStatus.DISMISSED
        return None
