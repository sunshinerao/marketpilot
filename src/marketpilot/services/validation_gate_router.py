from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from marketpilot.domain.decision import DecisionAction
from marketpilot.validation.promotion_gate import (
    FrozenValidationReport,
    PromotionCriteria,
    ValidationSliceEvidence,
    evaluate_promotion_gate,
)


class PromotionCriteriaOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["LOCAL"] = "LOCAL"
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    criteria_id: str
    version: str
    registered_at: datetime
    required_slices: tuple[tuple[str, str], ...]
    minimum_samples_per_slice: int
    maximum_expiry_cross_rate: float
    maximum_touch_rate: float
    maximum_cvar: float
    maximum_drawdown: float
    minimum_no_trade_pnl_difference: float
    criteria_hash: str
    live_eligible: bool = False

    @classmethod
    def from_domain(cls, value: PromotionCriteria) -> PromotionCriteriaOutput:
        value.verify()
        return cls(**asdict(value))


class ValidationSliceEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    event_type: str = Field(min_length=1)
    regime: str = Field(min_length=1)
    sample_count: int = Field(ge=0)
    expiry_cross_rate: float = Field(ge=0, le=1)
    touch_rate: float = Field(ge=0, le=1)
    cvar: float = Field(ge=0)
    maximum_drawdown: float = Field(ge=0)
    no_trade_pnl_difference: float

    def to_domain(self) -> ValidationSliceEvidence:
        return ValidationSliceEvidence(**self.model_dump())


class PromotionGateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: Literal["SCENARIO"]
    scope: Literal["LOCAL"]
    data_manifest_hash: str = Field(min_length=1)
    holdout_manifest_hash: str = Field(min_length=1)
    holdout_frozen_at: datetime
    evaluated_at: datetime
    slices: tuple[ValidationSliceEvidenceInput, ...] = Field(min_length=1, max_length=100)

    @field_validator("holdout_frozen_at", "evaluated_at")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("validation timestamps must be timezone-aware")
        return value


class PromotionGateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: Literal["SCENARIO"] = "SCENARIO"
    scope: Literal["LOCAL"] = "LOCAL"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    passed: bool
    local_promotion_evidence_available: bool
    live_eligible: bool = False
    failures: tuple[str, ...]
    criteria_hash: str
    data_manifest_hash: str
    holdout_manifest_hash: str
    evaluated_at: datetime
    report_hash: str

    @classmethod
    def from_domain(cls, value: FrozenValidationReport) -> PromotionGateOutput:
        value.verify()
        return cls(
            passed=value.passed,
            local_promotion_evidence_available=value.passed,
            failures=value.failures,
            criteria_hash=value.criteria_hash,
            data_manifest_hash=value.data_manifest_hash,
            holdout_manifest_hash=value.holdout_manifest_hash,
            evaluated_at=value.evaluated_at,
            report_hash=value.report_hash,
        )


def create_validation_gate_router(criteria: PromotionCriteria) -> APIRouter:
    criteria.verify()
    router = APIRouter(prefix="/v1/validation", tags=["validation"])

    @router.get("/promotion-criteria", response_model=PromotionCriteriaOutput)
    def promotion_criteria() -> PromotionCriteriaOutput:
        return PromotionCriteriaOutput.from_domain(criteria)

    @router.post("/promotion-gate", response_model=PromotionGateOutput)
    def promotion_gate(request: PromotionGateInput) -> PromotionGateOutput:
        try:
            report = evaluate_promotion_gate(
                criteria,
                data_manifest_hash=request.data_manifest_hash,
                holdout_manifest_hash=request.holdout_manifest_hash,
                holdout_frozen_at=request.holdout_frozen_at,
                evaluated_at=request.evaluated_at,
                slices=tuple(item.to_domain() for item in request.slices),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        return PromotionGateOutput.from_domain(report)

    return router
