from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from marketpilot.domain.alerts import AlertRecord
from marketpilot.domain.decision import DecisionAction


class StreamEventKind(StrEnum):
    ALERT_STATE = "ALERT_STATE"


class DeliveryOutcome(StrEnum):
    ATTEMPTED = "ATTEMPTED"


class AlertStreamEvent(BaseModel):
    """Immutable server-side projection emitted on the alert SSE stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    kind: StreamEventKind = StreamEventKind.ALERT_STATE
    recorded_at: datetime
    alert: AlertRecord
    run_mode: Literal["SCENARIO"] = "SCENARIO"
    scope: Literal["LOCAL"] = "LOCAL"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE

    @field_validator("recorded_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        return value


class DeliveryAuditRecord(BaseModel):
    """Append-only evidence of an attempt, never a claim of client receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: str
    connection_id: str = Field(min_length=1)
    stream_event_id: str = Field(min_length=1)
    attempted_at: datetime
    outcome: DeliveryOutcome = DeliveryOutcome.ATTEMPTED

    @field_validator("attempted_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("attempted_at must be timezone-aware")
        return value
