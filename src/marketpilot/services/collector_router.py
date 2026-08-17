from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from marketpilot.domain.capabilities import CapabilityReport
from marketpilot.domain.collector import (
    CollectorEvent,
    CollectorEventKind,
    CollectorPolicy,
    CollectorState,
)
from marketpilot.domain.contracts import ExplicitESContract
from marketpilot.domain.data_quality import QualityPolicy
from marketpilot.domain.decision import DecisionAction
from marketpilot.domain.market import DataQuality
from marketpilot.domain.point_in_time import PointInTimeRecord
from marketpilot.domain.trading_calendar import (
    NEW_YORK,
    EquityCalendarConfig,
    GlobexCalendarConfig,
    GlobexSessionClock,
    SessionState,
    TradingDayStatus,
    USEquityCalendar,
)
from marketpilot.services.collector_service import CollectorOrchestrator
from marketpilot.services.session_quality_router import QuoteObservationInput

RunMode = Literal["SCENARIO", "LOCAL"]


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value


class CollectorCapabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    configured: bool
    quality: DataQuality
    verification_status: Literal["VERIFIED", "SCHEMA_ONLY", "UNVERIFIED"]
    production_ready: bool

    def to_domain(self, *, probed_at: datetime) -> CapabilityReport:
        return CapabilityReport(
            provider=self.provider,
            probed_at=probed_at,
            sdk_version="scenario",
            environment="scenario",
            configured=self.configured,
            quality=self.quality,
            verification_status=self.verification_status,
            production_ready=self.production_ready,
            unverified_requirements=() if self.verification_status == "VERIFIED" else ("SCENARIO",),
            results=(),
        )


class CollectorPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_backoff_seconds: float = Field(default=1, gt=0)
    max_backoff_seconds: float = Field(default=30, gt=0)
    max_reconnect_attempts: int = Field(default=5, ge=1)
    allowed_lateness_seconds: float = Field(default=0.25, ge=0)
    freshness_limit_seconds: float = Field(default=5, gt=0)
    green_max_age_seconds: float = Field(default=2, ge=0)
    amber_max_age_seconds: float = Field(default=5, ge=0)
    max_receive_latency_seconds: float = Field(default=1, ge=0)
    conflict_absolute_tolerance: Decimal = Field(default=Decimal("0.50"), ge=0)
    conflict_relative_tolerance: Decimal = Field(default=Decimal("0.0001"), ge=0)
    require_two_sources: bool = True

    @model_validator(mode="after")
    def validate_thresholds(self) -> CollectorPolicyInput:
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds must be at least base_backoff_seconds")
        if self.amber_max_age_seconds < self.green_max_age_seconds:
            raise ValueError("amber_max_age_seconds must be at least green_max_age_seconds")
        return self

    def collector_policy(self) -> CollectorPolicy:
        return CollectorPolicy(
            base_backoff=timedelta(seconds=self.base_backoff_seconds),
            max_backoff=timedelta(seconds=self.max_backoff_seconds),
            max_reconnect_attempts=self.max_reconnect_attempts,
            allowed_lateness=timedelta(seconds=self.allowed_lateness_seconds),
            freshness_limit=timedelta(seconds=self.freshness_limit_seconds),
        )

    def quality_policy(self) -> QualityPolicy:
        return QualityPolicy(
            green_max_age=timedelta(seconds=self.green_max_age_seconds),
            amber_max_age=timedelta(seconds=self.amber_max_age_seconds),
            max_receive_latency=timedelta(seconds=self.max_receive_latency_seconds),
            conflict_absolute_tolerance=self.conflict_absolute_tolerance,
            conflict_relative_tolerance=self.conflict_relative_tolerance,
            require_two_sources=self.require_two_sources,
        )


class EarlyCloseConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_date: date
    closes_at: time


class EquitySessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["EQUITY"]
    instrument_id: str = Field(min_length=1)
    verified_from: date
    verified_through: date
    holidays: tuple[date, ...] = ()
    early_closes: tuple[EarlyCloseConfigInput, ...] = ()

    @model_validator(mode="after")
    def unique_early_closes(self) -> EquitySessionInput:
        dates = [item.session_date for item in self.early_closes]
        if len(dates) != len(set(dates)):
            raise ValueError("early-close dates must be unique")
        return self


class ESSessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["ES"]
    symbol: str = Field(min_length=1)
    expiry: date
    verified_from: date
    verified_through: date
    closed_dates: tuple[date, ...] = ()


SessionInput = Annotated[EquitySessionInput | ESSessionInput, Field(discriminator="kind")]


class EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    published_at: datetime
    first_seen_at: datetime
    schema_version: str = Field(min_length=1)

    @field_validator("published_at", "first_seen_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class StartEventInput(EventBase):
    kind: Literal[CollectorEventKind.START]


class ConnectedEventInput(EventBase):
    kind: Literal[CollectorEventKind.CONNECTED]


class HeartbeatEventInput(EventBase):
    kind: Literal[CollectorEventKind.HEARTBEAT]


class ConnectionLostEventInput(EventBase):
    kind: Literal[CollectorEventKind.CONNECTION_LOST]


class StopEventInput(EventBase):
    kind: Literal[CollectorEventKind.STOP]


class SchemaDriftEventInput(EventBase):
    kind: Literal[CollectorEventKind.SCHEMA_DRIFT]
    observed_schema_version: str = Field(min_length=1)


class RateLimitedEventInput(EventBase):
    kind: Literal[CollectorEventKind.RATE_LIMITED]
    retry_after_seconds: float = Field(gt=0)


class QuoteEventInput(EventBase):
    kind: Literal[CollectorEventKind.QUOTE]
    observation: QuoteObservationInput


EventInput = Annotated[
    StartEventInput
    | ConnectedEventInput
    | HeartbeatEventInput
    | ConnectionLostEventInput
    | StopEventInput
    | SchemaDriftEventInput
    | RateLimitedEventInput
    | QuoteEventInput,
    Field(discriminator="kind"),
]


class CollectorRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: RunMode
    provider: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    expected_schema_version: str = Field(min_length=1)
    capability: CollectorCapabilityInput
    session: SessionInput
    policy: CollectorPolicyInput = Field(default_factory=CollectorPolicyInput)
    events: tuple[EventInput, ...] = Field(min_length=1, max_length=1000)


class PointInTimeRecordOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    logical_key: str
    published_at: datetime
    first_seen_at: datetime
    provider: str
    provider_version: str
    schema_version: str
    content_hash: str

    @classmethod
    def from_domain(cls, record: PointInTimeRecord) -> PointInTimeRecordOutput:
        return cls(
            record_id=record.record_id,
            logical_key=record.logical_key,
            published_at=record.published_at,
            first_seen_at=record.first_seen_at,
            provider=record.provider,
            provider_version=record.provider_version,
            schema_version=record.schema_version,
            content_hash=record.content_hash,
        )


class CollectorTraceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    state: CollectorState
    accepted: bool
    reasons: tuple[str, ...]
    input_record_id: str
    input_content_hash: str
    output_record_id: str
    output_content_hash: str
    watermark: datetime | None
    next_retry_at: datetime | None
    reconnect_attempts: int
    quality: DataQuality
    freeze: bool


class CollectorRunOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: RunMode
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    execution_enabled: Literal[False] = False
    action: Literal[DecisionAction.NO_TRADE] = DecisionAction.NO_TRADE
    state: CollectorState
    permits_decision: bool
    reasons: tuple[str, ...]
    accepted_quotes: int
    duplicate_events: int
    duplicate_observations: int
    out_of_order_observations: int
    watermark: datetime | None
    next_retry_at: datetime | None
    traces: tuple[CollectorTraceOutput, ...]
    records: tuple[PointInTimeRecordOutput, ...]


def _event_to_domain(value: EventInput) -> CollectorEvent:
    payload: dict[str, object] = {}
    observation = None
    if isinstance(value, RateLimitedEventInput):
        payload["retry_after_seconds"] = value.retry_after_seconds
    elif isinstance(value, SchemaDriftEventInput):
        payload["observed_schema_version"] = value.observed_schema_version
    elif isinstance(value, QuoteEventInput):
        observation = value.observation.to_domain()
    return CollectorEvent(
        event_id=value.event_id,
        kind=CollectorEventKind(value.kind),
        published_at=value.published_at,
        first_seen_at=value.first_seen_at,
        schema_version=value.schema_version,
        payload=payload,
        observation=observation,
    )


def _session_gate(value: SessionInput) -> Callable[[datetime], tuple[str, ...]]:
    if isinstance(value, EquitySessionInput):
        calendar = USEquityCalendar(
            EquityCalendarConfig(
                verified_from=value.verified_from,
                verified_through=value.verified_through,
                holidays=frozenset(value.holidays),
                early_closes={item.session_date: item.closes_at for item in value.early_closes},
            )
        )

        def equity_gate(instant: datetime) -> tuple[str, ...]:
            local = calendar.session(instant.astimezone(NEW_YORK).date())
            if local.status is not TradingDayStatus.TRADING_DAY:
                return (f"EQUITY_SESSION_{local.status.value}",)
            if local.opens_at is None or local.closes_at is None:
                return ("EQUITY_SESSION_UNVERIFIED",)
            localized = instant.astimezone(local.opens_at.tzinfo)
            if not local.opens_at <= localized <= local.closes_at:
                return ("EQUITY_SESSION_CLOSED",)
            return ()

        return equity_gate

    ExplicitESContract(symbol=value.symbol, expiry=value.expiry)
    clock = GlobexSessionClock(
        GlobexCalendarConfig(
            verified_from=value.verified_from,
            verified_through=value.verified_through,
            closed_dates=frozenset(value.closed_dates),
        )
    )

    def es_gate(instant: datetime) -> tuple[str, ...]:
        session_state = clock.state_at(instant)
        if session_state is SessionState.OPEN:
            return ()
        return (f"GLOBEX_SESSION_{session_state.value}",)

    return es_gate


def create_collector_router() -> APIRouter:
    router = APIRouter(prefix="/v1/scenario/collector", tags=["scenario-collector"])

    @router.post("/run", response_model=CollectorRunOutput)
    def run_collector(request: CollectorRunInput) -> CollectorRunOutput:
        try:
            events = tuple(_event_to_domain(event) for event in request.events)
            capability = request.capability.to_domain(probed_at=events[0].first_seen_at)
            result = CollectorOrchestrator(
                provider=request.provider,
                provider_version=request.provider_version,
                expected_schema_version=request.expected_schema_version,
                expected_instrument_id=(
                    request.session.instrument_id
                    if isinstance(request.session, EquitySessionInput)
                    else ExplicitESContract(
                        symbol=request.session.symbol, expiry=request.session.expiry
                    ).symbol
                ),
                capability=capability,
                policy=request.policy.collector_policy(),
                quality_policy=request.policy.quality_policy(),
                session_gate=_session_gate(request.session),
            ).run(events)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        return CollectorRunOutput(
            run_mode=request.run_mode,
            state=result.state,
            permits_decision=result.permits_decision,
            reasons=result.reasons,
            accepted_quotes=result.accepted_quotes,
            duplicate_events=result.duplicate_events,
            duplicate_observations=result.duplicate_observations,
            out_of_order_observations=result.out_of_order_observations,
            watermark=result.watermark,
            next_retry_at=result.next_retry_at,
            traces=tuple(
                CollectorTraceOutput(
                    event_id=trace.event_id,
                    state=trace.state,
                    accepted=trace.accepted,
                    reasons=trace.reasons,
                    input_record_id=trace.input_record_id,
                    input_content_hash=trace.input_content_hash,
                    output_record_id=trace.output_record_id,
                    output_content_hash=trace.output_content_hash,
                    watermark=trace.watermark,
                    next_retry_at=trace.next_retry_at,
                    reconnect_attempts=trace.reconnect_attempts,
                    quality=trace.quality,
                    freeze=trace.freeze,
                )
                for trace in result.traces
            ),
            records=tuple(PointInTimeRecordOutput.from_domain(record) for record in result.records),
        )

    return router


router = create_collector_router()
