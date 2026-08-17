from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from marketpilot.domain.decision import DecisionAction, NoTradeReason
from marketpilot.validation.execution_economics import (
    ExecutableValue,
    ExecutionAssumptions,
    ExecutionFailure,
    LegNbbo,
    value_opening_execution,
)
from marketpilot.validation.risk_economics import (
    ConservativeRiskContract,
    PnlScenario,
    RiskEligibility,
    assess_entry_risk,
)


class LegNbboInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    leg_id: str = Field(min_length=1)
    quantity: int
    multiplier: float = Field(gt=0)
    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    bid_size: int | None = Field(default=None, ge=0)
    ask_size: int | None = Field(default=None, ge=0)
    quoted_at: datetime

    @field_validator("quoted_at")
    @classmethod
    def quoted_at_must_be_aware(cls, value: datetime) -> datetime:
        return _aware("quoted_at", value)

    def to_domain(self) -> LegNbbo:
        return LegNbbo(**self.model_dump())


class ExecutionAssumptionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    max_quote_age_seconds: float = Field(ge=0)
    fee_per_contract: float = Field(ge=0)
    slippage_per_contract: float = Field(ge=0)
    max_size_participation: float = Field(default=1.0, gt=0, le=1)

    def to_domain(self) -> ExecutionAssumptions:
        return ExecutionAssumptions(
            max_quote_age=timedelta(seconds=self.max_quote_age_seconds),
            fee_per_contract=self.fee_per_contract,
            slippage_per_contract=self.slippage_per_contract,
            max_size_participation=self.max_size_participation,
        )


class PnlScenarioInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    name: str = Field(min_length=1)
    probability: float = Field(gt=0, le=1)
    conservative_pnl: float

    def to_domain(self) -> PnlScenario:
        return PnlScenario(**self.model_dump())


class RiskContractInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    max_loss: float | None = Field(default=None, ge=0)
    risk_budget: float = Field(ge=0)
    cvar_budget: float = Field(ge=0)
    cvar_confidence: float = Field(default=0.95, gt=0, lt=1)

    def to_domain(self) -> ConservativeRiskContract:
        return ConservativeRiskContract(**self.model_dump())


class EconomicAssessmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: Literal["SCENARIO"]
    scope: Literal["LOCAL"]
    valued_at: datetime
    quotes: tuple[LegNbboInput, ...] = Field(min_length=1, max_length=16)
    assumptions: ExecutionAssumptionsInput
    scenarios: tuple[PnlScenarioInput, ...] = Field(min_length=1, max_length=1000)
    risk: RiskContractInput

    @field_validator("valued_at")
    @classmethod
    def valued_at_must_be_aware(cls, value: datetime) -> datetime:
        return _aware("valued_at", value)


class ExecutedLegOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    leg_id: str
    quantity: int
    execution_price: float
    half_spread_cost: float
    slippage_cost: float
    fee: float


class ExecutableValueOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    valued_at: datetime
    is_executable: bool
    failures: tuple[ExecutionFailure, ...]
    legs: tuple[ExecutedLegOutput, ...]
    gross_cashflow: float | None
    fees: float | None
    net_cashflow: float | None

    @classmethod
    def from_domain(cls, value: ExecutableValue) -> ExecutableValueOutput:
        return cls(
            valued_at=value.valued_at,
            is_executable=value.is_executable,
            failures=value.failures,
            legs=tuple(ExecutedLegOutput(**asdict(leg)) for leg in value.legs),
            gross_cashflow=value.gross_cashflow,
            fees=value.fees,
            net_cashflow=value.net_cashflow,
        )


class RiskEligibilityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: DecisionAction
    risk_gate_cleared: bool
    quote_executable: bool
    max_loss: float | None
    scenario_max_loss: float
    scenario_expected_pnl: float
    tail_loss_cvar: float
    reason: NoTradeReason | None

    @classmethod
    def from_domain(cls, value: RiskEligibility) -> RiskEligibilityOutput:
        return cls(
            action=value.action,
            risk_gate_cleared=value.entry_eligible,
            quote_executable=value.quote_executable,
            max_loss=value.max_loss,
            scenario_max_loss=value.scenario_max_loss,
            scenario_expected_pnl=value.scenario_expected_pnl,
            tail_loss_cvar=value.tail_loss_cvar,
            reason=value.reason,
        )


class EconomicAssessmentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: Literal["SCENARIO"] = "SCENARIO"
    scope: Literal["LOCAL"] = "LOCAL"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    execution_enabled: bool = False
    manual_execution_only: bool = True
    action: DecisionAction
    opening_value: ExecutableValueOutput
    risk: RiskEligibilityOutput


def _aware(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def create_economics_router() -> APIRouter:
    router = APIRouter(prefix="/v1/scenario/economics", tags=["scenario-economics"])

    @router.post("/assess", response_model=EconomicAssessmentOutput)
    def assess(request: EconomicAssessmentInput) -> EconomicAssessmentOutput:
        try:
            opening = value_opening_execution(
                tuple(item.to_domain() for item in request.quotes),
                valued_at=request.valued_at,
                assumptions=request.assumptions.to_domain(),
            )
            risk = assess_entry_risk(
                opening,
                tuple(item.to_domain() for item in request.scenarios),
                request.risk.to_domain(),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        return EconomicAssessmentOutput(
            action=risk.action,
            opening_value=ExecutableValueOutput.from_domain(opening),
            risk=RiskEligibilityOutput.from_domain(risk),
        )

    return router


router = create_economics_router()
