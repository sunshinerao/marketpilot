from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from marketpilot.domain.alert_delivery import (
    AttemptOutcome,
    DeliveryAttempt,
    DeliveryChannel,
    OutboxEvent,
    OutboxMessage,
)
from marketpilot.domain.decision import DecisionAction
from marketpilot.services.alert_delivery_service import (
    AlertDeliveryOrchestrator,
    AlertDeliveryPolicy,
    ChannelPolicy,
    ScriptedDeliverySender,
    SenderResult,
)
from marketpilot.services.alert_delivery_store import AlertDeliveryStore


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value


class ChannelPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: DeliveryChannel
    enabled: bool = False
    opted_in: bool = False
    authorization_id: str | None = None
    allow_network: bool = False
    cooldown_seconds: float = Field(default=300, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    base_backoff_seconds: float = Field(default=5, gt=0)
    max_backoff_seconds: float = Field(default=120, gt=0)
    require_ack: bool = False
    ack_timeout_seconds: float = Field(default=120, gt=0)
    max_escalations: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def retry_bounds_are_valid(self) -> ChannelPolicyInput:
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds must be at least base_backoff_seconds")
        return self

    def to_domain(self) -> ChannelPolicy:
        return ChannelPolicy(
            channel=self.channel,
            enabled=self.enabled,
            opted_in=self.opted_in,
            authorization_id=self.authorization_id,
            # The scenario router never authorizes network use, even if this describes
            # a future production policy.
            allow_network=self.allow_network,
            cooldown=timedelta(seconds=self.cooldown_seconds),
            max_attempts=self.max_attempts,
            base_backoff=timedelta(seconds=self.base_backoff_seconds),
            max_backoff=timedelta(seconds=self.max_backoff_seconds),
            require_ack=self.require_ack,
            ack_timeout=timedelta(seconds=self.ack_timeout_seconds),
            max_escalations=self.max_escalations,
        )


class SenderOutcomeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: AttemptOutcome
    reason_code: str | None = None
    response_reference: str | None = None


class SenderScriptInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: DeliveryChannel
    outcomes: tuple[SenderOutcomeInput, ...] = Field(default=(), max_length=100)


class EnqueueOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["ENQUEUE"]
    message_id: str = Field(min_length=1)
    alert_id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    channel: DeliveryChannel
    destination_reference: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    payload: dict[str, object]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class DispatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["DISPATCH"]
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class AcknowledgeOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["ACKNOWLEDGE"]
    message_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    acknowledged_at: datetime

    @field_validator("acknowledged_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class EscalateOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["ESCALATE"]
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware(value)


DeliveryOperation = Annotated[
    EnqueueOperation | DispatchOperation | AcknowledgeOperation | EscalateOperation,
    Field(discriminator="kind"),
]


class AlertDeliveryRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: Literal["SCENARIO"]
    scope: Literal["LOCAL"]
    policy_version: str = Field(min_length=1)
    channels: tuple[ChannelPolicyInput, ...] = Field(default=(), max_length=3)
    sender_mode: Literal["NULL", "SCRIPTED"] = "NULL"
    sender_scripts: tuple[SenderScriptInput, ...] = Field(default=(), max_length=3)
    operations: tuple[DeliveryOperation, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_channels_and_safe_sender_mode(self) -> AlertDeliveryRunInput:
        channels = [item.channel for item in self.channels]
        if len(channels) != len(set(channels)):
            raise ValueError("channel policies must be unique")
        scripts = [item.channel for item in self.sender_scripts]
        if len(scripts) != len(set(scripts)):
            raise ValueError("sender scripts must be unique")
        if self.sender_mode == "NULL" and self.sender_scripts:
            raise ValueError("NULL sender mode must not include sender scripts")
        if self.sender_mode == "SCRIPTED" and not self.sender_scripts:
            raise ValueError("SCRIPTED sender mode requires sender scripts")
        return self


class AlertDeliveryRunOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_mode: Literal["SCENARIO"] = "SCENARIO"
    scope: Literal["LOCAL"] = "LOCAL"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    execution_enabled: Literal[False] = False
    action: Literal[DecisionAction.NO_TRADE] = DecisionAction.NO_TRADE
    network_authorized: Literal[False] = False
    sender_mode: Literal["NULL", "SCRIPTED"]
    messages: tuple[OutboxMessage, ...]
    events: tuple[OutboxEvent, ...]
    attempts: tuple[DeliveryAttempt, ...]


def create_alert_delivery_router() -> APIRouter:
    router = APIRouter(prefix="/v1/scenario/alert-delivery", tags=["scenario-alert-delivery"])

    @router.post("/run", response_model=AlertDeliveryRunOutput)
    def run_delivery(request: AlertDeliveryRunInput) -> AlertDeliveryRunOutput:
        store = AlertDeliveryStore()
        try:
            policy = AlertDeliveryPolicy(
                version=request.policy_version,
                channels={item.channel: item.to_domain() for item in request.channels},
            )
            senders = {
                script.channel: ScriptedDeliverySender(
                    script.channel,
                    tuple(
                        SenderResult(
                            outcome=item.outcome,
                            reason_code=item.reason_code,
                            response_reference=item.response_reference,
                        )
                        for item in script.outcomes
                    ),
                )
                for script in request.sender_scripts
            }
            service = AlertDeliveryOrchestrator(
                store=store,
                policy=policy,
                signing_key=b"marketpilot-scenario-non-secret-signing-key-v1",
                senders=senders,
                # This router is deliberately incapable of authorizing a network sender.
                network_authorized=False,
            )
            for operation in request.operations:
                if isinstance(operation, EnqueueOperation):
                    service.enqueue(
                        message_id=operation.message_id,
                        alert_id=operation.alert_id,
                        fingerprint=operation.fingerprint,
                        channel=operation.channel,
                        destination_reference=operation.destination_reference,
                        payload=operation.payload,
                        created_at=operation.created_at,
                    )
                elif isinstance(operation, DispatchOperation):
                    service.dispatch_due(operation.as_of)
                elif isinstance(operation, AcknowledgeOperation):
                    service.acknowledge(
                        operation.message_id,
                        actor=operation.actor,
                        acknowledged_at=operation.acknowledged_at,
                    )
                else:
                    service.escalate_due(operation.as_of)
            return AlertDeliveryRunOutput(
                sender_mode=request.sender_mode,
                messages=store.messages(),
                events=store.events(),
                attempts=store.attempts(),
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        finally:
            store.close()

    return router


router = create_alert_delivery_router()
