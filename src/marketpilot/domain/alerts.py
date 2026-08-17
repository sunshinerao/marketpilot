from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AlertPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class AlertStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    DISMISSED = "DISMISSED"


class AlertDirection(StrEnum):
    UPSIDE = "UPSIDE"
    DOWNSIDE = "DOWNSIDE"
    TWO_SIDED = "TWO_SIDED"
    NON_DIRECTIONAL = "NON_DIRECTIONAL"


class AlertAction(StrEnum):
    NO_TRADE = "NO_TRADE"
    RISK_LOCK = "RISK_LOCK"
    RERUN = "RERUN"


class FeedbackKind(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    DISMISSED = "DISMISSED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    MANUAL_JUDGMENT = "MANUAL_JUDGMENT"
    EXECUTION = "EXECUTION"
    FILL = "FILL"


BoundedAlertIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:+-]*$"),
]


class AlertCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fingerprint: BoundedAlertIdentifier
    priority: AlertPriority
    direction: AlertDirection
    observed_at: datetime
    evidence: tuple[BoundedAlertIdentifier, ...] = Field(default=(), max_length=50)
    event_id: BoundedAlertIdentifier | None = None
    snapshot_id: BoundedAlertIdentifier
    model_version: BoundedAlertIdentifier
    rules_version: BoundedAlertIdentifier
    action: AlertAction
    invalidation_conditions: tuple[BoundedAlertIdentifier, ...] = Field(
        default=(), max_length=50
    )
    rerun_trigger: BoundedAlertIdentifier

    @field_validator("observed_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class AlertRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_id: BoundedAlertIdentifier
    candidate: AlertCandidate
    created_at: datetime
    status: AlertStatus = AlertStatus.OPEN
    deduplicated_count: int = 0
    escalation_level: int = 0

    @field_validator("created_at")
    @classmethod
    def created_at_timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value


class AlertFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_id: BoundedAlertIdentifier
    alert_id: BoundedAlertIdentifier
    kind: FeedbackKind
    recorded_at: datetime
    actor: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("recorded_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        return value
