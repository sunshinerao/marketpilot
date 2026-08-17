from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from marketpilot.domain.alerts import AlertFeedback, AlertRecord
from marketpilot.domain.attribution import AttributionReview, AttributionTask
from marketpilot.domain.governance import GovernanceApproval, GovernanceEvent, ModelVersion
from marketpilot.domain.point_in_time import PointInTimeRecord, ReplayManifest
from marketpilot.domain.streaming import AlertStreamEvent, DeliveryAuditRecord
from marketpilot.services.repository import (
    AuditIntegrityReport,
    PointInTimeRecordMetadata,
    ReplayManifestMetadata,
)
from marketpilot.services.schemas import DecisionRunOutput


@runtime_checkable
class AuditRepository(Protocol):
    """Storage contract shared by local SQLite and production adapters."""

    def close(self) -> None: ...

    def append_decision(self, decision: DecisionRunOutput) -> None: ...

    def get_decision(self, run_id: str) -> DecisionRunOutput | None: ...

    def decisions(self, *, limit: int = 100) -> tuple[DecisionRunOutput, ...]: ...

    def append_alert(self, alert: AlertRecord) -> None: ...

    def alerts(self) -> tuple[AlertRecord, ...]: ...

    def get_alert(self, alert_id: str) -> AlertRecord | None: ...

    def append_feedback(self, feedback: AlertFeedback) -> None: ...

    def feedback(self, alert_id: str | None = None) -> tuple[AlertFeedback, ...]: ...

    def append_point_in_time_record(self, record: PointInTimeRecord) -> None: ...

    def point_in_time_records(self) -> tuple[PointInTimeRecordMetadata, ...]: ...

    def append_replay_manifest(self, manifest: ReplayManifest) -> None: ...

    def replay_manifests(self) -> tuple[ReplayManifestMetadata, ...]: ...

    def schema_version(self) -> str: ...

    def integrity_check(self) -> AuditIntegrityReport: ...


@dataclass(frozen=True, slots=True)
class RecoveryCheckpoint:
    checkpoint_id: str
    captured_at: datetime
    database_lsn: str
    backup_reference: str
    manifest_hash: str
    code_version: str
    schema_version: str


class RecoveryRepository(Protocol):
    def append_recovery_checkpoint(self, checkpoint: RecoveryCheckpoint) -> None: ...

    def latest_recovery_checkpoint(self) -> RecoveryCheckpoint | None: ...


@runtime_checkable
class StreamAttributionRepository(Protocol):
    """Shared contract for SQLite and PostgreSQL stream/attribution stores."""

    def close(self) -> None: ...

    def append_alert_projection(
        self,
        *,
        projection_key: str,
        alert: AlertRecord,
        recorded_at: datetime,
    ) -> AlertStreamEvent: ...

    def stream_events_after(self, event_id: str | None) -> tuple[AlertStreamEvent, ...]: ...

    def validate_cursor(self, event_id: str | None) -> int: ...

    def append_delivery(self, record: DeliveryAuditRecord) -> None: ...

    def deliveries(
        self, stream_event_id: str | None = None
    ) -> tuple[DeliveryAuditRecord, ...]: ...

    def append_attribution_task(self, task: AttributionTask) -> AttributionTask: ...

    def task_by_signal(self, signal_id: str) -> AttributionTask | None: ...

    def get_attribution_task(self, task_id: str) -> AttributionTask | None: ...

    def attribution_tasks(self) -> tuple[AttributionTask, ...]: ...

    def append_attribution_review(self, review: AttributionReview) -> None: ...

    def attribution_reviews(self, task_id: str) -> tuple[AttributionReview, ...]: ...


@runtime_checkable
class ChampionRegistry(Protocol):
    """Registry behavior shared by process-local and durable governance backends."""

    def register_challenger(self, model: ModelVersion) -> None: ...

    def promote(
        self, model_id: str, version: str, approval: GovernanceApproval
    ) -> ModelVersion: ...

    def freeze_session(self, model_id: str, session_id: str) -> ModelVersion: ...

    def champion(self, model_id: str, *, session_id: str | None = None) -> ModelVersion: ...

    def rollback(
        self,
        model_id: str,
        target_version: str,
        approval: GovernanceApproval,
    ) -> ModelVersion: ...

    def lineage(self, model_id: str, version: str) -> tuple[ModelVersion, ...]: ...

    def audit_events(self) -> tuple[GovernanceEvent, ...]: ...

    def versions(self, model_id: str) -> tuple[ModelVersion, ...]: ...


@dataclass(frozen=True, slots=True)
class GovernanceSessionFreeze:
    model_id: str
    session_id: str
    version: str
    frozen_at: datetime


@runtime_checkable
class GovernancePersistenceRepository(Protocol):
    """Append-only model governance storage, separate from policy orchestration."""

    def close(self) -> None: ...

    def append_model_version(self, model: ModelVersion) -> None: ...

    def model_versions(self, model_id: str) -> tuple[ModelVersion, ...]: ...

    def model_version(self, model_id: str, version: str) -> ModelVersion | None: ...

    def append_action(
        self, approval: GovernanceApproval, event: GovernanceEvent
    ) -> None: ...

    def current_champion(self, model_id: str) -> ModelVersion | None: ...

    def events(self) -> tuple[GovernanceEvent, ...]: ...

    def approvals(self) -> tuple[GovernanceApproval, ...]: ...

    def freeze_session(
        self,
        model_id: str,
        session_id: str,
        *,
        frozen_at: datetime,
    ) -> GovernanceSessionFreeze: ...

    def session_freeze(
        self, model_id: str, session_id: str
    ) -> GovernanceSessionFreeze | None: ...


class Cursor(Protocol):
    rowcount: int

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Cursor: ...

    def fetchone(self) -> Any | None: ...

    def fetchall(self) -> list[Any]: ...

    def close(self) -> None: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[], Connection]
