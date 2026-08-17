from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from marketpilot.domain.snapshot import freeze_snapshot


class GovernanceError(ValueError):
    """Raised when a model-governance invariant is violated."""


def _required(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise GovernanceError(f"{field_name} must not be blank")
    return normalized


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GovernanceError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ModelVersion:
    model_id: str
    version: str
    artifact_hash: str
    data_manifest_hash: str
    trained_at: datetime
    validation_report_hash: str | None = None
    parent_version: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("model_id", "version", "artifact_hash", "data_manifest_hash"):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        object.__setattr__(self, "trained_at", _utc(self.trained_at, "trained_at"))
        if self.validation_report_hash is not None:
            object.__setattr__(
                self,
                "validation_report_hash",
                _required(self.validation_report_hash, "validation_report_hash"),
            )
        if self.parent_version is not None:
            object.__setattr__(
                self, "parent_version", _required(self.parent_version, "parent_version")
            )


class ApprovalAction(StrEnum):
    PROMOTE = "PROMOTE"
    ROLLBACK = "ROLLBACK"


@dataclass(frozen=True, slots=True)
class GovernanceApproval:
    approval_id: str
    action: ApprovalAction
    model_id: str
    source_version: str | None
    target_version: str
    approved_by: str
    approved_at: datetime
    evidence_hash: str
    note: str

    @classmethod
    def create(
        cls,
        *,
        action: ApprovalAction,
        model_id: str,
        source_version: str | None,
        target_version: str,
        approved_by: str,
        approved_at: datetime,
        evidence_hash: str,
        note: str,
    ) -> GovernanceApproval:
        timestamp = _utc(approved_at, "approved_at")
        values = {
            "model_id": _required(model_id, "model_id"),
            "target_version": _required(target_version, "target_version"),
            "approved_by": _required(approved_by, "approved_by"),
            "evidence_hash": _required(evidence_hash, "evidence_hash"),
            "note": _required(note, "note"),
        }
        source = _required(source_version, "source_version") if source_version else None
        identity = freeze_snapshot(
            {
                "action": action,
                "model_id": values["model_id"],
                "source_version": source,
                "target_version": values["target_version"],
                "approved_by": values["approved_by"],
                "approved_at": timestamp,
                "evidence_hash": values["evidence_hash"],
                "note": values["note"],
            }
        ).snapshot_id
        return cls(
            approval_id=identity,
            action=action,
            model_id=values["model_id"],
            source_version=source,
            target_version=values["target_version"],
            approved_by=values["approved_by"],
            approved_at=timestamp,
            evidence_hash=values["evidence_hash"],
            note=values["note"],
        )

    def verify(self) -> None:
        expected = freeze_snapshot(
            {
                "action": self.action,
                "model_id": self.model_id,
                "source_version": self.source_version,
                "target_version": self.target_version,
                "approved_by": self.approved_by,
                "approved_at": self.approved_at,
                "evidence_hash": self.evidence_hash,
                "note": self.note,
            }
        ).snapshot_id
        if expected != self.approval_id:
            raise GovernanceError("governance approval identity mismatch")


@dataclass(frozen=True, slots=True)
class GovernanceEvent:
    action: ApprovalAction
    model_id: str
    source_version: str | None
    target_version: str
    approval_id: str
    occurred_at: datetime
