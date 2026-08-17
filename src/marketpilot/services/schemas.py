from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from marketpilot.domain.alerts import AlertFeedback, AlertRecord, FeedbackKind
from marketpilot.domain.decision import DecisionAction, NoTradeReason
from marketpilot.domain.events import EventRecord, RiskLockAssessment
from marketpilot.domain.market import DataQuality


class GateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_quality: DataQuality = DataQuality.RED
    contract_matches: bool = True
    event_cleared: bool = False
    unscheduled_shock: bool = False
    option_chain_usable: bool = False
    tail_expanding: bool = False
    next_major_event_in_holding_period: bool = False
    edge_ok: bool = False
    risk_budget_ok: bool = True


class DecisionRunMode(StrEnum):
    LIVE = "LIVE"
    SCENARIO = "SCENARIO"


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


class DecisionRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = "strikepilot_spxw_0dte_ic"
    run_mode: DecisionRunMode = DecisionRunMode.LIVE
    as_of: datetime | None = None
    scenario_session_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    values: dict[str, Any] = Field(default_factory=dict)
    gates: GateInput | None = None

    @field_validator("as_of")
    @classmethod
    def require_timezone_aware_as_of(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_gate_source(self) -> DecisionRunInput:
        if self.run_mode is DecisionRunMode.LIVE and self.gates is not None:
            raise ValueError("gates are server-derived for LIVE runs")
        if self.run_mode is DecisionRunMode.LIVE and self.values:
            raise ValueError("values are server-derived for LIVE runs")
        if self.run_mode is DecisionRunMode.LIVE and self.as_of is not None:
            raise ValueError("as_of is server-derived for LIVE runs")
        if self.run_mode is DecisionRunMode.LIVE and self.scenario_session_id is not None:
            raise ValueError("scenario_session_id is forbidden for LIVE runs")
        if self.run_mode is DecisionRunMode.SCENARIO and self.gates is None:
            raise ValueError("gates are required for SCENARIO runs")
        if self.run_mode is DecisionRunMode.SCENARIO and self.as_of is None:
            raise ValueError("as_of is required for SCENARIO runs")
        if self.run_mode is DecisionRunMode.SCENARIO and self.scenario_session_id is None:
            raise ValueError("scenario_session_id is required for SCENARIO runs")
        return self


class DecisionRunOutput(BaseModel):
    run_id: str
    platform: str = "MarketPilot"
    run_mode: DecisionRunMode
    execution_enabled: bool = False
    model_id: str
    model_version: str
    model_artifact_hash: str | None = None
    governed_model_version: str | None = None
    governed_model_artifact_hash: str | None = None
    governance_session_id: str | None = None
    rules_version: str
    code_version: str
    snapshot_id: str
    data_as_of: datetime
    action: DecisionAction
    reasons: list[NoTradeReason]
    output: dict[str, Any]


class OverviewComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    detail: str
    item_count: int | None = None


class OverviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = "MarketPilot"
    code_version: str
    as_of: datetime
    run_mode: DecisionRunMode = DecisionRunMode.LIVE
    verification: VerificationStatus = VerificationStatus.UNVERIFIED
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    reasons: tuple[str, ...]
    market: OverviewComponent
    replay: OverviewComponent
    risk_lock: OverviewComponent
    alerts: OverviewComponent


class ReplayManifestEntryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_key: str
    record_id: str
    content_hash: str


class ReplayManifestOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime
    manifest_hash: str
    entries: tuple[ReplayManifestEntryOutput, ...]


class DemoScenarioOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    title: str
    summary: str
    run_mode: DecisionRunMode = DecisionRunMode.SCENARIO
    verification: VerificationStatus = VerificationStatus.UNVERIFIED
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    event: EventRecord
    assessment: RiskLockAssessment
    replay_manifest: ReplayManifestOutput


class EventAssessmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_mode: DecisionRunMode
    as_of: datetime
    event: EventRecord

    @field_validator("as_of")
    @classmethod
    def require_timezone_aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value

    @model_validator(mode="after")
    def require_scenario_mode(self) -> EventAssessmentInput:
        if self.run_mode is not DecisionRunMode.SCENARIO:
            raise ValueError("event assessment input is scenario-only")
        return self


class EventAssessmentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_mode: DecisionRunMode = DecisionRunMode.SCENARIO
    verification: VerificationStatus = VerificationStatus.UNVERIFIED
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    assessment: RiskLockAssessment


class AlertListOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_mode: DecisionRunMode = DecisionRunMode.SCENARIO
    verification: VerificationStatus = VerificationStatus.UNVERIFIED
    execution_enabled: bool = False
    alerts: tuple[AlertRecord, ...]


class AlertFeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: FeedbackKind
    actor: str = Field(min_length=1, max_length=100)
    recorded_at: datetime
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("recorded_at")
    @classmethod
    def require_timezone_aware_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        return value


class AlertFeedbackOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_mode: DecisionRunMode = DecisionRunMode.SCENARIO
    verification: VerificationStatus = VerificationStatus.UNVERIFIED
    execution_enabled: bool = False
    feedback: AlertFeedback
    alert: AlertRecord


class AlertFeedbackListOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_mode: DecisionRunMode = DecisionRunMode.SCENARIO
    verification: VerificationStatus = VerificationStatus.UNVERIFIED
    execution_enabled: bool = False
    feedback: tuple[AlertFeedback, ...]
