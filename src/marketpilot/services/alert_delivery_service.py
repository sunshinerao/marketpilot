from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Protocol

from marketpilot.domain.alert_delivery import (
    AttemptOutcome,
    DeliveryAttempt,
    DeliveryChannel,
    OutboxEvent,
    OutboxEventKind,
    OutboxMessage,
    deterministic_delivery_id,
    sign_outbox_content,
    verify_outbox_message,
)
from marketpilot.services.alert_delivery_store import AlertDeliveryStore

_DESTINATION_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ChannelPolicy:
    channel: DeliveryChannel
    enabled: bool = False
    opted_in: bool = False
    authorization_id: str | None = None
    allow_network: bool = False
    cooldown: timedelta = timedelta(minutes=5)
    max_attempts: int = 3
    base_backoff: timedelta = timedelta(seconds=5)
    max_backoff: timedelta = timedelta(minutes=2)
    require_ack: bool = False
    ack_timeout: timedelta = timedelta(minutes=2)
    max_escalations: int = 0

    def __post_init__(self) -> None:
        if self.enabled and self.opted_in and not (self.authorization_id or "").strip():
            raise ValueError("enabled opted-in channels require authorization_id")
        if self.cooldown < timedelta(0):
            raise ValueError("cooldown must not be negative")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.base_backoff <= timedelta(0) or self.max_backoff < self.base_backoff:
            raise ValueError("invalid retry backoff bounds")
        if self.ack_timeout <= timedelta(0):
            raise ValueError("ack_timeout must be positive")
        if self.max_escalations < 0:
            raise ValueError("max_escalations must not be negative")

    @property
    def delivery_enabled(self) -> bool:
        return self.enabled and self.opted_in and bool((self.authorization_id or "").strip())

    def retry_delay(self, attempt_number: int) -> timedelta:
        if attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        seconds = self.base_backoff.total_seconds() * (2 ** (attempt_number - 1))
        return min(timedelta(seconds=seconds), self.max_backoff)


@dataclass(frozen=True, slots=True)
class AlertDeliveryPolicy:
    version: str
    channels: Mapping[DeliveryChannel, ChannelPolicy]

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("policy version must not be blank")
        if any(key is not value.channel for key, value in self.channels.items()):
            raise ValueError("channel policy keys must match their channel")
        object.__setattr__(self, "channels", MappingProxyType(dict(self.channels)))

    def for_channel(self, channel: DeliveryChannel) -> ChannelPolicy:
        return self.channels.get(channel, ChannelPolicy(channel=channel))


@dataclass(frozen=True, slots=True)
class SenderResult:
    outcome: AttemptOutcome
    reason_code: str | None = None
    response_reference: str | None = None


class DeliverySender(Protocol):
    name: str
    channel: DeliveryChannel
    network_capable: bool

    def send(self, message: OutboxMessage) -> SenderResult: ...


class NullDeliverySender:
    """Default sender. It performs no I/O and always records a disabled attempt."""

    name = "null-disabled"
    network_capable = False

    def __init__(self, channel: DeliveryChannel) -> None:
        self.channel = channel

    def send(self, message: OutboxMessage) -> SenderResult:
        del message
        return SenderResult(AttemptOutcome.DISABLED, reason_code="NULL_SENDER_DISABLED")


class ScriptedDeliverySender:
    """Local deterministic failure injector; never performs network I/O."""

    name = "scripted-local"
    network_capable = False

    def __init__(self, channel: DeliveryChannel, outcomes: Sequence[SenderResult]) -> None:
        self.channel = channel
        self._outcomes = deque(outcomes)
        self.calls = 0

    def send(self, message: OutboxMessage) -> SenderResult:
        del message
        self.calls += 1
        if not self._outcomes:
            return SenderResult(AttemptOutcome.DISABLED, reason_code="SCRIPT_EXHAUSTED")
        return self._outcomes.popleft()


class AlertDeliveryOrchestrator:
    """Append-only alert delivery coordinator with an explicit network authorization gate."""

    def __init__(
        self,
        *,
        store: AlertDeliveryStore,
        policy: AlertDeliveryPolicy,
        signing_key: bytes,
        senders: Mapping[DeliveryChannel, DeliverySender] | None = None,
        network_authorized: bool = False,
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        self.store = store
        self.policy = policy
        self._signing_key = signing_key
        self._senders = dict(senders or {})
        self._network_authorized = network_authorized
        for channel, sender in self._senders.items():
            if channel is not sender.channel:
                raise ValueError("sender mapping key must match sender channel")

    def enqueue(
        self,
        *,
        message_id: str,
        alert_id: str,
        fingerprint: str,
        channel: DeliveryChannel,
        destination_reference: str,
        payload: Mapping[str, object],
        created_at: datetime,
    ) -> OutboxMessage:
        created = _aware(created_at, "created_at")
        for name, value in (
            ("message_id", message_id),
            ("alert_id", alert_id),
            ("fingerprint", fingerprint),
            ("destination_reference", destination_reference),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        if _DESTINATION_REFERENCE.fullmatch(destination_reference) is None:
            raise ValueError("destination_reference must be an opaque allowlisted identifier")
        dedupe_key = f"{channel.value}:{fingerprint}:{destination_reference}"
        existing_by_id = self.store.message(message_id)
        previous = self.store.latest_message_for_dedupe(dedupe_key)
        channel_policy = self.policy.for_channel(channel)
        suppressed = False
        if previous is not None and previous.message_id != message_id:
            elapsed = created - previous.created_at
            if elapsed < timedelta(0):
                raise ValueError("message created_at regressed behind prior dedupe record")
            suppressed = elapsed < channel_policy.cooldown
        authorization_hash = (
            deterministic_delivery_id(
                "sha256", {"authorization_id": channel_policy.authorization_id}
            )
            if channel_policy.authorization_id
            else None
        )
        signing_content = {
            "message_id": message_id,
            "alert_id": alert_id,
            "fingerprint": fingerprint,
            "channel": channel.value,
            "destination_reference": destination_reference,
            "payload": dict(payload),
            "created_at": created,
            "dedupe_key": dedupe_key,
            "policy_version": self.policy.version,
            "authorization_reference_hash": authorization_hash,
            "run_mode": "SCENARIO",
        }
        message = OutboxMessage(
            message_id=message_id,
            alert_id=alert_id,
            fingerprint=fingerprint,
            channel=channel,
            destination_reference=destination_reference,
            payload=dict(payload),
            created_at=created,
            dedupe_key=dedupe_key,
            policy_version=self.policy.version,
            authorization_reference_hash=authorization_hash,
            run_mode="SCENARIO",
            signature=sign_outbox_content(signing_content, self._signing_key),
        )
        stored = self.store.append_message(message)
        if existing_by_id is not None:
            return stored
        if suppressed:
            self._append_event(
                message_id=stored.message_id,
                kind=OutboxEventKind.SUPPRESSED,
                recorded_at=created,
                reason_code="DEDUPE_COOLDOWN",
            )
            return stored
        if not channel_policy.delivery_enabled:
            self._append_event(
                message_id=stored.message_id,
                kind=OutboxEventKind.DELIVERY_DISABLED,
                recorded_at=created,
                reason_code="CHANNEL_NOT_OPTED_IN",
            )
            return stored
        self._append_event(
            message_id=stored.message_id,
            kind=OutboxEventKind.ENQUEUED,
            recorded_at=created,
        )
        return stored

    def dispatch_due(self, as_of: datetime) -> tuple[DeliveryAttempt, ...]:
        now = _aware(as_of, "as_of")
        attempts: list[DeliveryAttempt] = []
        for message in self.store.messages():
            if not self._is_due(message.message_id, now):
                continue
            attempts.append(self._dispatch(message, now))
        return tuple(attempts)

    def acknowledge(self, message_id: str, *, actor: str, acknowledged_at: datetime) -> OutboxEvent:
        now = _aware(acknowledged_at, "acknowledged_at")
        if not actor.strip():
            raise ValueError("actor must not be blank")
        if self.store.message(message_id) is None:
            raise KeyError(f"unknown outbox message: {message_id}")
        events = self.store.events(message_id)
        if not any(event.kind is OutboxEventKind.DELIVERED for event in events):
            raise ValueError("message must be delivered before acknowledgment")
        latest_delivery = next(
            event for event in reversed(events) if event.kind is OutboxEventKind.DELIVERED
        )
        if now < latest_delivery.recorded_at:
            raise ValueError("acknowledged_at must not precede delivery")
        existing = next(
            (event for event in events if event.kind is OutboxEventKind.ACKNOWLEDGED), None
        )
        if existing is not None:
            return existing
        return self._append_event(
            message_id=message_id,
            kind=OutboxEventKind.ACKNOWLEDGED,
            recorded_at=now,
            reason_code="USER_ACKNOWLEDGED",
            actor=actor,
        )

    def escalate_due(self, as_of: datetime) -> tuple[OutboxEvent, ...]:
        now = _aware(as_of, "as_of")
        escalations: list[OutboxEvent] = []
        for message in self.store.messages():
            channel_policy = self.policy.for_channel(message.channel)
            if not channel_policy.require_ack:
                continue
            events = self.store.events(message.message_id)
            if any(event.kind is OutboxEventKind.ACKNOWLEDGED for event in events):
                continue
            delivered = [event for event in events if event.kind is OutboxEventKind.DELIVERED]
            if not delivered:
                continue
            # An escalation already awaiting dispatch must not consume another level.
            if events[-1].kind is not OutboxEventKind.DELIVERED:
                continue
            latest = delivered[-1]
            if latest.ack_due_at is None or now < latest.ack_due_at:
                continue
            level = sum(event.kind is OutboxEventKind.ESCALATED for event in events)
            if level >= channel_policy.max_escalations:
                if not any(event.kind is OutboxEventKind.DEAD_LETTERED for event in events):
                    escalations.append(
                        self._append_event(
                            message_id=message.message_id,
                            kind=OutboxEventKind.DEAD_LETTERED,
                            recorded_at=now,
                            reason_code="ACK_TIMEOUT",
                            escalation_level=level,
                        )
                    )
                continue
            level += 1
            escalations.append(
                self._append_event(
                    message_id=message.message_id,
                    kind=OutboxEventKind.ESCALATED,
                    recorded_at=now,
                    reason_code="ACK_TIMEOUT",
                    escalation_level=level,
                )
            )
            attempt_number = len(self.store.attempts(message.message_id)) + 1
            self._append_event(
                message_id=message.message_id,
                kind=OutboxEventKind.RETRY_SCHEDULED,
                recorded_at=now,
                attempt_number=attempt_number,
                next_attempt_at=now,
                reason_code="ACK_ESCALATION",
                escalation_level=level,
            )
        return tuple(escalations)

    def verify_signature(self, message_id: str) -> bool:
        message = self.store.message(message_id)
        if message is None:
            raise KeyError(f"unknown outbox message: {message_id}")
        return verify_outbox_message(message, self._signing_key)

    def _is_due(self, message_id: str, now: datetime) -> bool:
        events = self.store.events(message_id)
        terminal = {
            OutboxEventKind.SUPPRESSED,
            OutboxEventKind.DELIVERY_DISABLED,
            OutboxEventKind.DEAD_LETTERED,
            OutboxEventKind.ACKNOWLEDGED,
        }
        if any(event.kind in terminal for event in events):
            return False
        if not events:
            return False
        latest = events[-1]
        if latest.kind is OutboxEventKind.ENQUEUED:
            return latest.recorded_at <= now
        return (
            latest.kind is OutboxEventKind.RETRY_SCHEDULED
            and latest.next_attempt_at is not None
            and latest.next_attempt_at <= now
        )

    def _dispatch(self, message: OutboxMessage, now: datetime) -> DeliveryAttempt:
        channel_policy = self.policy.for_channel(message.channel)
        sender = self._senders.get(message.channel, NullDeliverySender(message.channel))
        attempt_number = len(self.store.attempts(message.message_id)) + 1
        if not channel_policy.delivery_enabled:
            result = SenderResult(AttemptOutcome.DISABLED, "CHANNEL_NOT_OPTED_IN")
        elif not verify_outbox_message(message, self._signing_key):
            result = SenderResult(AttemptOutcome.PERMANENT_FAILURE, "SIGNATURE_INVALID")
        elif sender.network_capable and not (
            self._network_authorized and channel_policy.allow_network
        ):
            result = SenderResult(AttemptOutcome.DISABLED, "NETWORK_NOT_AUTHORIZED")
        else:
            try:
                result = sender.send(message)
            except Exception:  # sender errors are sanitized before append-only audit
                result = SenderResult(AttemptOutcome.TRANSIENT_FAILURE, "SENDER_EXCEPTION")
        attempt = DeliveryAttempt(
            attempt_id=deterministic_delivery_id(
                "attempt",
                {
                    "message_id": message.message_id,
                    "attempt_number": attempt_number,
                    "attempted_at": now,
                },
            ),
            message_id=message.message_id,
            channel=message.channel,
            attempted_at=now,
            attempt_number=attempt_number,
            sender_name=sender.name,
            outcome=result.outcome,
            reason_code=result.reason_code,
            response_reference=result.response_reference,
        )
        self.store.append_attempt(attempt)
        if result.outcome is AttemptOutcome.SENT:
            self._append_event(
                message_id=message.message_id,
                kind=OutboxEventKind.DELIVERED,
                recorded_at=now,
                attempt_number=attempt_number,
                ack_due_at=(now + channel_policy.ack_timeout)
                if channel_policy.require_ack
                else None,
            )
        elif result.outcome is AttemptOutcome.TRANSIENT_FAILURE:
            if attempt_number >= channel_policy.max_attempts:
                self._append_event(
                    message_id=message.message_id,
                    kind=OutboxEventKind.DEAD_LETTERED,
                    recorded_at=now,
                    attempt_number=attempt_number,
                    reason_code="RETRY_BUDGET_EXHAUSTED",
                )
            else:
                self._append_event(
                    message_id=message.message_id,
                    kind=OutboxEventKind.RETRY_SCHEDULED,
                    recorded_at=now,
                    attempt_number=attempt_number + 1,
                    next_attempt_at=now + channel_policy.retry_delay(attempt_number),
                    reason_code=result.reason_code or "TRANSIENT_FAILURE",
                )
        elif result.outcome is AttemptOutcome.PERMANENT_FAILURE:
            self._append_event(
                message_id=message.message_id,
                kind=OutboxEventKind.DEAD_LETTERED,
                recorded_at=now,
                attempt_number=attempt_number,
                reason_code=result.reason_code or "PERMANENT_FAILURE",
            )
        else:
            self._append_event(
                message_id=message.message_id,
                kind=OutboxEventKind.DELIVERY_DISABLED,
                recorded_at=now,
                attempt_number=attempt_number,
                reason_code=result.reason_code or "DELIVERY_DISABLED",
            )
        return attempt

    def _append_event(
        self,
        *,
        message_id: str,
        kind: OutboxEventKind,
        recorded_at: datetime,
        attempt_number: int | None = None,
        reason_code: str | None = None,
        next_attempt_at: datetime | None = None,
        ack_due_at: datetime | None = None,
        escalation_level: int = 0,
        actor: str | None = None,
    ) -> OutboxEvent:
        content = {
            "message_id": message_id,
            "kind": kind.value,
            "recorded_at": recorded_at,
            "attempt_number": attempt_number,
            "reason_code": reason_code,
            "next_attempt_at": next_attempt_at,
            "ack_due_at": ack_due_at,
            "escalation_level": escalation_level,
            "actor": actor,
        }
        event = OutboxEvent(
            event_id=deterministic_delivery_id("outbox-event", content),
            message_id=message_id,
            kind=kind,
            recorded_at=recorded_at,
            attempt_number=attempt_number,
            reason_code=reason_code,
            next_attempt_at=next_attempt_at,
            ack_due_at=ack_due_at,
            escalation_level=escalation_level,
            actor=actor,
        )
        return self.store.append_event(event)


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
