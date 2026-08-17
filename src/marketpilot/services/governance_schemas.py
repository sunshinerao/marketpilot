from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from marketpilot.domain.decision import DecisionAction
from marketpilot.domain.governance import ApprovalAction


class ApprovalRunMode(StrEnum):
    SCENARIO = "SCENARIO"


class ApprovalScope(StrEnum):
    LOCAL = "LOCAL"


class CalibrationStatus(StrEnum):
    NOT_CALIBRATED = "NOT_CALIBRATED"
    LOCAL_VALIDATION_AVAILABLE = "LOCAL_VALIDATION_AVAILABLE"


class GovernanceApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: ApprovalRunMode
    scope: ApprovalScope
    source_version: str | None = Field(default=None, min_length=1)
    target_version: str = Field(min_length=1)
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    evidence_hash: str = Field(min_length=1)
    note: str = Field(min_length=1)

    @field_validator(
        "source_version", "target_version", "approved_by", "evidence_hash", "note"
    )
    @classmethod
    def values_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("approved_at")
    @classmethod
    def approved_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approved_at must be timezone-aware")
        return value


class ChallengerRegistrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: ApprovalRunMode
    scope: ApprovalScope
    version: str = Field(min_length=1)
    artifact_hash: str = Field(min_length=1)
    data_manifest_hash: str = Field(min_length=1)
    trained_at: datetime
    validation_report_hash: str | None = Field(default=None, min_length=1)
    parent_version: str | None = Field(default=None, min_length=1)

    @field_validator(
        "version",
        "artifact_hash",
        "data_manifest_hash",
        "validation_report_hash",
        "parent_version",
    )
    @classmethod
    def registration_values_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("trained_at")
    @classmethod
    def trained_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("trained_at must be timezone-aware")
        return value

class ModelVersionOutput(BaseModel):
    model_id: str
    version: str
    artifact_hash: str
    data_manifest_hash: str
    trained_at: datetime
    validation_report_hash: str | None
    parent_version: str | None
    is_local_champion: bool
    calibration_status: CalibrationStatus
    deployment_scope: ApprovalScope = ApprovalScope.LOCAL
    live_eligible: bool = False


class ModelVersionsOutput(BaseModel):
    model_id: str
    versions: list[ModelVersionOutput]
    live_enabled: bool = False


class ChallengerRegistrationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: ApprovalRunMode = ApprovalRunMode.SCENARIO
    scope: ApprovalScope = ApprovalScope.LOCAL
    verification: str = "UNVERIFIED"
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    model: ModelVersionOutput
    live_eligible: bool = False


class ChampionOutput(BaseModel):
    model_id: str
    champion: ModelVersionOutput
    session_id: str | None = None
    frozen_for_session: bool = False
    live_enabled: bool = False


class GovernanceActionOutput(BaseModel):
    action: ApprovalAction
    approval_id: str
    champion: ChampionOutput
    live_enabled: bool = False
    message: str


class NoTradeEffectOutput(BaseModel):
    eligible_count: int
    no_trade_count: int
    entered_count: int
    filtered_pnl_total: float
    unfiltered_counterfactual_pnl_total: float
    no_trade_counterfactual_pnl_total: float
    pnl_difference: float


class ValidationSliceOutput(BaseModel):
    strata: list[tuple[str, str]]
    sample_count: int
    action_counts: dict[DecisionAction, int]
    metric_means: dict[str, float]
    no_trade_effect: NoTradeEffectOutput


class ValidationSummaryOutput(BaseModel):
    model_id: str
    calibration_status: CalibrationStatus
    report_hash: str | None
    slices: list[ValidationSliceOutput]
    conclusion: str = "NO_AUTOMATIC_PERFORMANCE_CONCLUSION"
    live_eligible: bool = False
