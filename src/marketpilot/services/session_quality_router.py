from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from marketpilot.domain.data_quality import (
    EntitlementStatus,
    QualityPolicy,
    QuoteObservation,
    QuoteQualityEvaluator,
)
from marketpilot.domain.decision import DecisionAction
from marketpilot.domain.market import DataQuality
from marketpilot.domain.trading_calendar import (
    EquityCalendarConfig,
    GlobexCalendarConfig,
    GlobexSessionClock,
    SessionState,
    TradingDayStatus,
    USEquityCalendar,
)

ScenarioRunMode = Literal["SCENARIO"]
VerificationStatus = Literal["UNVERIFIED"]


class EarlyCloseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_date: date
    closes_at: time


class EquitySessionEvaluationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: ScenarioRunMode
    session_date: date
    verified_from: date
    verified_through: date
    holidays: tuple[date, ...] = Field(default=(), max_length=400)
    early_closes: tuple[EarlyCloseInput, ...] = Field(default=(), max_length=400)

    @model_validator(mode="after")
    def unique_early_close_dates(self) -> EquitySessionEvaluationInput:
        dates = [item.session_date for item in self.early_closes]
        if len(set(dates)) != len(dates):
            raise ValueError("early-close session dates must be unique")
        return self


class EquitySessionEvaluationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: ScenarioRunMode = "SCENARIO"
    verification: VerificationStatus = "UNVERIFIED"
    execution_enabled: Literal[False] = False
    action: Literal[DecisionAction.NO_TRADE] = DecisionAction.NO_TRADE
    session_date: date
    status: TradingDayStatus
    opens_at: datetime | None
    preopen_cutoff_at: datetime | None
    closes_at: datetime | None
    anchor_at: datetime | None
    is_early_close: bool
    permits_trading: bool


class GlobexSessionEvaluationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: ScenarioRunMode
    instant: datetime
    verified_from: date
    verified_through: date
    closed_dates: tuple[date, ...] = Field(default=(), max_length=400)

    @field_validator("instant")
    @classmethod
    def instant_must_be_aware(cls, value: datetime) -> datetime:
        return _aware_datetime("instant", value)


class GlobexSessionEvaluationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: ScenarioRunMode = "SCENARIO"
    verification: VerificationStatus = "UNVERIFIED"
    execution_enabled: Literal[False] = False
    action: Literal[DecisionAction.NO_TRADE] = DecisionAction.NO_TRADE
    state: SessionState


class QuoteObservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    source_ts: datetime
    received_ts: datetime
    delayed: bool | None
    entitlement: EntitlementStatus
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    field_timestamps: dict[str, datetime] = Field(max_length=32)

    @field_validator("source_ts", "received_ts")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime, info: object) -> datetime:
        # Pydantic's ValidationInfo is intentionally not coupled to the domain layer.
        field_name = getattr(info, "field_name", "timestamp")
        return _aware_datetime(str(field_name), value)

    @field_validator("field_timestamps")
    @classmethod
    def field_timestamps_must_be_aware(
        cls,
        value: dict[str, datetime],
    ) -> dict[str, datetime]:
        for name, timestamp in value.items():
            _aware_datetime(f"field_timestamps[{name}]", timestamp)
        return value

    def to_domain(self) -> QuoteObservation:
        return QuoteObservation(
            source=self.source,
            instrument_id=self.instrument_id,
            source_ts=self.source_ts,
            received_ts=self.received_ts,
            delayed=self.delayed,
            entitlement=self.entitlement,
            bid=self.bid,
            ask=self.ask,
            bid_size=self.bid_size,
            ask_size=self.ask_size,
            field_timestamps=self.field_timestamps,
        )


class QualityPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    green_max_age_seconds: float = Field(ge=0)
    amber_max_age_seconds: float = Field(ge=0)
    max_receive_latency_seconds: float = Field(ge=0)
    conflict_absolute_tolerance: Decimal = Field(ge=0)
    conflict_relative_tolerance: Decimal = Field(default=Decimal("0"), ge=0)
    require_two_sources: bool = True

    @model_validator(mode="after")
    def amber_threshold_must_cover_green(self) -> QualityPolicyInput:
        if self.amber_max_age_seconds < self.green_max_age_seconds:
            raise ValueError("amber_max_age_seconds must be at least green_max_age_seconds")
        return self

    def to_domain(self) -> QualityPolicy:
        return QualityPolicy(
            green_max_age=timedelta(seconds=self.green_max_age_seconds),
            amber_max_age=timedelta(seconds=self.amber_max_age_seconds),
            max_receive_latency=timedelta(seconds=self.max_receive_latency_seconds),
            conflict_absolute_tolerance=self.conflict_absolute_tolerance,
            conflict_relative_tolerance=self.conflict_relative_tolerance,
            require_two_sources=self.require_two_sources,
        )


class QuoteQualityEvaluationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: ScenarioRunMode
    as_of: datetime
    policy: QualityPolicyInput
    observations: tuple[QuoteObservationInput, ...] = Field(max_length=16)

    @field_validator("as_of")
    @classmethod
    def as_of_must_be_aware(cls, value: datetime) -> datetime:
        return _aware_datetime("as_of", value)


class QuoteQualityEvaluationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: ScenarioRunMode = "SCENARIO"
    verification: VerificationStatus = "UNVERIFIED"
    execution_enabled: Literal[False] = False
    action: Literal[DecisionAction.NO_TRADE] = DecisionAction.NO_TRADE
    quality: DataQuality
    freeze: bool
    permits_decision: bool
    reasons: tuple[str, ...]
    stale_fields: tuple[str, ...]
    sources: tuple[str, ...]
    observed_at: datetime


def _aware_datetime(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _unprocessable(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


def create_session_quality_router() -> APIRouter:
    """Build a read-only, SCENARIO-only router without mutating the application root."""

    router = APIRouter(prefix="/v1/scenario/session-quality", tags=["scenario-session-quality"])

    @router.post("/equity-session", response_model=EquitySessionEvaluationOutput)
    def equity_session(request: EquitySessionEvaluationInput) -> EquitySessionEvaluationOutput:
        try:
            calendar = USEquityCalendar(
                EquityCalendarConfig(
                    verified_from=request.verified_from,
                    verified_through=request.verified_through,
                    holidays=frozenset(request.holidays),
                    early_closes={
                        item.session_date: item.closes_at for item in request.early_closes
                    },
                )
            )
            session = calendar.session(request.session_date)
        except ValueError as exc:
            raise _unprocessable(exc) from exc
        return EquitySessionEvaluationOutput(
            session_date=session.session_date,
            status=session.status,
            opens_at=session.opens_at,
            preopen_cutoff_at=session.preopen_cutoff_at,
            closes_at=session.closes_at,
            anchor_at=session.anchor_at,
            is_early_close=session.is_early_close,
            permits_trading=session.permits_trading,
        )

    @router.post("/globex-session", response_model=GlobexSessionEvaluationOutput)
    def globex_session(request: GlobexSessionEvaluationInput) -> GlobexSessionEvaluationOutput:
        try:
            clock = GlobexSessionClock(
                GlobexCalendarConfig(
                    verified_from=request.verified_from,
                    verified_through=request.verified_through,
                    closed_dates=frozenset(request.closed_dates),
                )
            )
            state = clock.state_at(request.instant)
        except ValueError as exc:
            raise _unprocessable(exc) from exc
        return GlobexSessionEvaluationOutput(state=state)

    @router.post("/quote-quality", response_model=QuoteQualityEvaluationOutput)
    def quote_quality(request: QuoteQualityEvaluationInput) -> QuoteQualityEvaluationOutput:
        try:
            report = QuoteQualityEvaluator(request.policy.to_domain()).evaluate(
                tuple(item.to_domain() for item in request.observations),
                as_of=request.as_of,
            )
        except ValueError as exc:
            raise _unprocessable(exc) from exc
        return QuoteQualityEvaluationOutput(
            quality=report.status,
            freeze=report.freeze,
            permits_decision=report.permits_decision,
            reasons=report.reasons,
            stale_fields=report.stale_fields,
            sources=report.sources,
            observed_at=report.observed_at,
        )

    return router


router = create_session_quality_router()
