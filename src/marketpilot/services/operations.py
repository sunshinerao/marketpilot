from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from marketpilot.domain.alerts import (
    AlertAction,
    AlertCandidate,
    AlertDirection,
    AlertFeedback,
    AlertPriority,
    AlertRecord,
    AlertStatus,
    FeedbackKind,
)
from marketpilot.domain.events import EventRecord, RiskLockAssessment
from marketpilot.events.risk_lock import RiskLockEngine
from marketpilot.services.demo import DEMO_BASE, demo_scenario_artifacts
from marketpilot.services.persistence_contracts import AuditRepository
from marketpilot.services.repository import SQLiteAuditRepository
from marketpilot.services.schemas import DemoScenarioOutput, OverviewComponent, OverviewOutput


class OperationsService:
    """Local operations facade; it never enables execution or mutates live providers."""

    def __init__(
        self,
        repository: AuditRepository | None = None,
        *,
        seed_demo: bool = True,
        code_version: str = "development-unpinned",
    ) -> None:
        self._risk_lock = RiskLockEngine()
        self._repository = repository or SQLiteAuditRepository(":memory:")
        self._code_version = code_version
        if seed_demo:
            self._seed_demo_alert()

    def overview(
        self,
        *,
        as_of: datetime,
        market_quality: str,
        market_reason: str,
    ) -> OverviewOutput:
        scenarios = self.scenarios()
        return OverviewOutput(
            code_version=self._code_version,
            as_of=as_of.astimezone(UTC),
            reasons=(market_reason, "EVENT_SOURCE_NOT_AUTHORIZED", "MODEL_NOT_CALIBRATED"),
            market=OverviewComponent(
                status=market_quality,
                detail=market_reason,
            ),
            replay=OverviewComponent(
                status="DEMO_ONLY",
                detail="Point-in-time manifests are local deterministic fixtures.",
                item_count=len(scenarios),
            ),
            risk_lock=OverviewComponent(
                status="LOCKED",
                detail="No authorized live event source is configured.",
                item_count=sum(item.assessment.state != "CLEARED" for item in scenarios),
            ),
            alerts=OverviewComponent(
                status="DEMO_ONLY",
                detail="Alerts and feedback use the local append-only audit repository.",
                item_count=len(self.alerts()),
            ),
        )

    def assess_event(self, event: EventRecord, as_of: datetime) -> RiskLockAssessment:
        return self._risk_lock.assess(event, as_of)

    def scenarios(self) -> tuple[DemoScenarioOutput, ...]:
        artifacts = demo_scenario_artifacts()
        for artifact in artifacts:
            self._repository.append_point_in_time_record(artifact.record)
            self._repository.append_replay_manifest(artifact.manifest)
        return tuple(artifact.output for artifact in artifacts)

    def alerts(self) -> tuple[AlertRecord, ...]:
        return tuple(self._with_feedback_status(alert) for alert in self._repository.alerts())

    def feedback(self, alert_id: str) -> tuple[AlertFeedback, ...]:
        if self._repository.get_alert(alert_id) is None:
            raise KeyError(f"unknown alert: {alert_id}")
        return self._repository.feedback(alert_id)

    def record_feedback(
        self,
        *,
        alert_id: str,
        kind: FeedbackKind,
        actor: str,
        recorded_at: datetime,
        note: str | None,
    ) -> tuple[AlertFeedback, AlertRecord]:
        if self._repository.get_alert(alert_id) is None:
            raise KeyError(f"unknown alert: {alert_id}")
        feedback = AlertFeedback(
            feedback_id=str(uuid4()),
            alert_id=alert_id,
            kind=kind,
            recorded_at=recorded_at,
            actor=actor,
            note=note,
        )
        self._repository.append_feedback(feedback)
        alert = next(item for item in self.alerts() if item.alert_id == alert_id)
        return feedback, alert

    def _seed_demo_alert(self) -> None:
        observed_at = DEMO_BASE + timedelta(seconds=10)
        self._repository.append_alert(
            AlertRecord(
                alert_id="demo-alert-downside-tail",
                candidate=AlertCandidate(
                    fingerprint="demo-downside-tail",
                    priority=AlertPriority.P0,
                    direction=AlertDirection.DOWNSIDE,
                    observed_at=observed_at,
                    evidence=("ES_LIQUIDITY_DROP", "VIX_TERM_INVERSION"),
                    event_id="demo-shock-001",
                    snapshot_id="sha256:demo-unverified",
                    model_version="0.1.0-baseline",
                    rules_version="rules-v1",
                    action=AlertAction.NO_TRADE,
                    invalidation_conditions=("CROSS_ASSET_STABLE_2M",),
                    rerun_trigger="T+2m",
                ),
                created_at=observed_at,
            )
        )

    def _with_feedback_status(self, alert: AlertRecord) -> AlertRecord:
        status = alert.status
        for feedback in self._repository.feedback(alert.alert_id):
            if feedback.kind is FeedbackKind.ACKNOWLEDGED:
                status = AlertStatus.ACKNOWLEDGED
            elif feedback.kind in {FeedbackKind.DISMISSED, FeedbackKind.FALSE_POSITIVE}:
                status = AlertStatus.DISMISSED
        return alert.model_copy(update={"status": status})
