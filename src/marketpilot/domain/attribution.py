from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from marketpilot.domain.decision import DecisionAction
from marketpilot.domain.events import EventSeverity


class AttributionTriggerKind(StrEnum):
    MAJOR_EVENT = "MAJOR_EVENT"
    ABNORMAL_MOVE = "ABNORMAL_MOVE"


class AttributionReviewStatus(StrEnum):
    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class CrossAssetCoherence(StrEnum):
    UNKNOWN = "UNKNOWN"
    COHERENT = "COHERENT"
    MIXED = "MIXED"
    DIVERGENT = "DIVERGENT"


class ReactionTimingInterpretation(StrEnum):
    SIGNAL_PRECEDED_MARKET = "SIGNAL_PRECEDED_MARKET"
    MARKET_PRECEDED_SIGNAL = "MARKET_PRECEDED_SIGNAL"
    SIMULTANEOUS = "SIMULTANEOUS"


class CandidateCause(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cause_id: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=500)
    source_published_at: datetime
    first_seen_at: datetime
    confidence: float = Field(ge=0, le=1)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=50)

    @model_validator(mode="after")
    def validate_timeline(self) -> CandidateCause:
        for name, value in (
            ("source_published_at", self.source_published_at),
            ("first_seen_at", self.first_seen_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.source_published_at > self.first_seen_at:
            raise ValueError("source_published_at cannot be after first_seen_at")
        return self


class CrossAssetObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset: str = Field(min_length=1)
    observed_at: datetime
    move_bps: float
    coherent: bool
    snapshot_id: str = Field(min_length=1)

    @field_validator("observed_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value

    @field_validator("snapshot_id")
    @classmethod
    def snapshot_identity_required(cls, value: str) -> str:
        if not value.startswith("sha256:"):
            raise ValueError("snapshot_id must be a sha256 identity")
        return value


class AttributionSignal(BaseModel):
    """Structured trigger facts; narrative alone can never produce a trade."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_id: str = Field(min_length=1)
    kind: AttributionTriggerKind
    severity: EventSeverity
    observed_as_of: datetime
    first_seen_at: datetime
    market_reaction_start_at: datetime
    snapshot_id: str = Field(min_length=1)
    replay_manifest_hash: str = Field(min_length=1)
    candidates: tuple[CandidateCause, ...] = Field(default=(), max_length=50)
    cross_asset_observations: tuple[CrossAssetObservation, ...] = Field(
        default=(), max_length=50
    )

    @model_validator(mode="after")
    def validate_signal(self) -> AttributionSignal:
        for name, value in (
            ("observed_as_of", self.observed_as_of),
            ("first_seen_at", self.first_seen_at),
            ("market_reaction_start_at", self.market_reaction_start_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.kind is AttributionTriggerKind.MAJOR_EVENT and self.severity not in {
            EventSeverity.P0,
            EventSeverity.P1,
        }:
            raise ValueError("MAJOR_EVENT requires P0 or P1 severity")
        if not self.snapshot_id.startswith("sha256:"):
            raise ValueError("snapshot_id must be a sha256 identity")
        if not self.replay_manifest_hash.startswith("sha256:"):
            raise ValueError("replay_manifest_hash must be a sha256 identity")
        if self.first_seen_at > self.observed_as_of:
            raise ValueError("first_seen_at cannot be after observed_as_of")
        if self.market_reaction_start_at > self.observed_as_of:
            raise ValueError("market_reaction_start_at cannot be after observed_as_of")
        future_candidates = [
            candidate.cause_id
            for candidate in self.candidates
            if candidate.first_seen_at > self.observed_as_of
        ]
        if future_candidates:
            raise ValueError("candidate causes cannot use future first_seen_at values")
        if any(
            observation.observed_at > self.observed_as_of
            for observation in self.cross_asset_observations
        ):
            raise ValueError("cross-asset observations cannot be after observed_as_of")
        if len({candidate.cause_id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("candidate cause_id values must be unique")
        return self


class AttributionTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    signal: AttributionSignal
    created_at: datetime
    reaction_lag_seconds: float
    reaction_timing_interpretation: ReactionTimingInterpretation
    cross_asset_coherence: CrossAssetCoherence
    confidence: float = Field(ge=0, le=1)
    review_status: AttributionReviewStatus = AttributionReviewStatus.OPEN
    counterfactual_replay_link: str
    retained_as_reusable_sample: bool = False
    run_mode: Literal["SCENARIO"] = "SCENARIO"
    scope: Literal["LOCAL"] = "LOCAL"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE


class AttributionReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    task_id: str
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
