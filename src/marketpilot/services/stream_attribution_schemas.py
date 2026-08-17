from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from marketpilot.domain.attribution import (
    AttributionReview,
    AttributionReviewStatus,
    AttributionSignal,
    AttributionTask,
)
from marketpilot.domain.decision import DecisionAction
from marketpilot.domain.streaming import DeliveryAuditRecord


class DeliveryAuditListOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_mode: Literal["SCENARIO"] = "SCENARIO"
    scope: Literal["LOCAL"] = "LOCAL"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    deliveries: tuple[DeliveryAuditRecord, ...]


class AttributionTaskListOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_mode: Literal["SCENARIO"] = "SCENARIO"
    scope: Literal["LOCAL"] = "LOCAL"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    tasks: tuple[AttributionTask, ...]


class AttributionSignalInput(BaseModel):
    """Explicitly prevents live client facts from entering local attribution."""

    model_config = ConfigDict(extra="forbid")

    run_mode: Literal["SCENARIO"]
    scope: Literal["LOCAL"]
    signal: AttributionSignal


class AttributionReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AttributionReviewStatus
    reviewer: str = Field(min_length=1, max_length=100)
    reviewed_at: datetime
    note: str | None = Field(default=None, max_length=1000)
    retain_as_reusable_sample: bool = False

    @field_validator("reviewed_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reviewed_at must be timezone-aware")
        return value


class AttributionReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_mode: Literal["SCENARIO"] = "SCENARIO"
    scope: Literal["LOCAL"] = "LOCAL"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    review: AttributionReview
    task: AttributionTask


class AttributionReviewListOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_mode: Literal["SCENARIO"] = "SCENARIO"
    scope: Literal["LOCAL"] = "LOCAL"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    reviews: tuple[AttributionReview, ...]


class CounterfactualReplayOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_mode: Literal["SCENARIO"] = "SCENARIO"
    scope: Literal["LOCAL"] = "LOCAL"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    task_id: str
    as_of: datetime
    snapshot_id: str
    replay_manifest_hash: str
    exclude_signal_id: str
    purpose: str
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
