from __future__ import annotations

import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from marketpilot.domain.snapshot import freeze_snapshot


class DeliveryChannel(StrEnum):
    WEBHOOK = "WEBHOOK"
    EMAIL = "EMAIL"
    MOBILE = "MOBILE"


class OutboxEventKind(StrEnum):
    ENQUEUED = "ENQUEUED"
    SUPPRESSED = "SUPPRESSED"
    DELIVERY_DISABLED = "DELIVERY_DISABLED"
    DELIVERED = "DELIVERED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    DEAD_LETTERED = "DEAD_LETTERED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ESCALATED = "ESCALATED"


class AttemptOutcome(StrEnum):
    SENT = "SENT"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    DISABLED = "DISABLED"


class OutboxMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(min_length=1)
    alert_id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    channel: DeliveryChannel
    destination_reference: str = Field(min_length=1)
    payload: Mapping[str, Any]
    created_at: datetime
    dedupe_key: str = Field(min_length=1)
    signature: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1)
    authorization_reference_hash: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    run_mode: Literal["SCENARIO"]

    @field_validator("created_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "created_at")

    def signing_content(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "alert_id": self.alert_id,
            "fingerprint": self.fingerprint,
            "channel": self.channel.value,
            "destination_reference": self.destination_reference,
            "payload": dict(self.payload),
            "created_at": self.created_at,
            "dedupe_key": self.dedupe_key,
            "policy_version": self.policy_version,
            "authorization_reference_hash": self.authorization_reference_hash,
            "run_mode": self.run_mode,
        }


class OutboxEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    kind: OutboxEventKind
    recorded_at: datetime
    attempt_number: int | None = Field(default=None, ge=1)
    reason_code: str | None = None
    next_attempt_at: datetime | None = None
    ack_due_at: datetime | None = None
    escalation_level: int = Field(default=0, ge=0)
    actor: str | None = None

    @field_validator("recorded_at", "next_attempt_at", "ack_due_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "timestamp") if value is not None else None


class DeliveryAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    channel: DeliveryChannel
    attempted_at: datetime
    attempt_number: int = Field(ge=1)
    sender_name: str = Field(min_length=1)
    outcome: AttemptOutcome
    reason_code: str | None = None
    response_reference: str | None = None

    @field_validator("attempted_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "attempted_at")


def sign_outbox_content(content: Mapping[str, Any], signing_key: bytes) -> str:
    if not signing_key:
        raise ValueError("signing_key must not be empty")
    canonical = freeze_snapshot(content).canonical_json.encode("utf-8")
    digest = hmac.new(signing_key, canonical, sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def verify_outbox_message(message: OutboxMessage, signing_key: bytes) -> bool:
    expected = sign_outbox_content(message.signing_content(), signing_key)
    return hmac.compare_digest(message.signature, expected)


def deterministic_delivery_id(prefix: str, content: Mapping[str, Any]) -> str:
    return f"{prefix}:{freeze_snapshot(content).snapshot_id.removeprefix('sha256:')}"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
