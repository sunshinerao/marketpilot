from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from marketpilot.domain.data_quality import FeedQualityReport, QuoteObservation
from marketpilot.domain.market import DataQuality
from marketpilot.domain.point_in_time import PointInTimeRecord


class CollectorState(StrEnum):
    STOPPED = "STOPPED"
    CONNECTING = "CONNECTING"
    STREAMING = "STREAMING"
    BACKING_OFF = "BACKING_OFF"
    RATE_LIMITED = "RATE_LIMITED"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"


class CollectorEventKind(StrEnum):
    START = "START"
    CONNECTED = "CONNECTED"
    QUOTE = "QUOTE"
    HEARTBEAT = "HEARTBEAT"
    CONNECTION_LOST = "CONNECTION_LOST"
    RATE_LIMITED = "RATE_LIMITED"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    STOP = "STOP"


@dataclass(frozen=True, slots=True)
class CollectorPolicy:
    base_backoff: timedelta = timedelta(seconds=1)
    max_backoff: timedelta = timedelta(seconds=30)
    max_reconnect_attempts: int = 5
    allowed_lateness: timedelta = timedelta(milliseconds=250)
    freshness_limit: timedelta = timedelta(seconds=5)

    def __post_init__(self) -> None:
        if self.base_backoff <= timedelta(0):
            raise ValueError("base_backoff must be positive")
        if self.max_backoff < self.base_backoff:
            raise ValueError("max_backoff must be at least base_backoff")
        if self.max_reconnect_attempts < 1:
            raise ValueError("max_reconnect_attempts must be positive")
        if self.allowed_lateness < timedelta(0):
            raise ValueError("allowed_lateness must not be negative")
        if self.freshness_limit <= timedelta(0):
            raise ValueError("freshness_limit must be positive")

    def reconnect_delay(self, attempt: int) -> timedelta:
        if attempt < 1:
            raise ValueError("attempt must be positive")
        multiplier = 2 ** (attempt - 1)
        candidate = timedelta(seconds=self.base_backoff.total_seconds() * multiplier)
        return min(candidate, self.max_backoff)


@dataclass(frozen=True, slots=True)
class CollectorEvent:
    event_id: str
    kind: CollectorEventKind
    published_at: datetime
    first_seen_at: datetime
    schema_version: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    observation: QuoteObservation | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("schema_version", self.schema_version),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        published = _utc(self.published_at, "published_at")
        first_seen = _utc(self.first_seen_at, "first_seen_at")
        if published > first_seen:
            raise ValueError("published_at must be less than or equal to first_seen_at")
        if self.kind is CollectorEventKind.QUOTE and self.observation is None:
            raise ValueError("QUOTE events require an observation")
        if self.kind is not CollectorEventKind.QUOTE and self.observation is not None:
            raise ValueError("only QUOTE events may contain an observation")
        object.__setattr__(self, "published_at", published)
        object.__setattr__(self, "first_seen_at", first_seen)
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def point_in_time_record(self, *, provider: str, provider_version: str) -> PointInTimeRecord:
        content: dict[str, Any] = {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "payload": dict(self.payload),
        }
        if self.observation is not None:
            content["observation"] = {
                "source": self.observation.source,
                "instrument_id": self.observation.instrument_id,
                "source_ts": self.observation.source_ts,
                "received_ts": self.observation.received_ts,
                "delayed": self.observation.delayed,
                "entitlement": self.observation.entitlement.value,
                "bid": self.observation.bid,
                "ask": self.observation.ask,
                "bid_size": self.observation.bid_size,
                "ask_size": self.observation.ask_size,
                "field_timestamps": dict(self.observation.field_timestamps),
            }
        return PointInTimeRecord.create(
            logical_key=f"collector-event:{provider}:{self.event_id}",
            published_at=self.published_at,
            first_seen_at=self.first_seen_at,
            provider=provider,
            provider_version=provider_version,
            schema_version=self.schema_version,
            content=content,
        )


@dataclass(frozen=True, slots=True)
class CollectorTrace:
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


@dataclass(frozen=True, slots=True)
class CollectorRunResult:
    state: CollectorState
    traces: tuple[CollectorTrace, ...]
    records: tuple[PointInTimeRecord, ...]
    quality_report: FeedQualityReport | None
    reasons: tuple[str, ...]
    accepted_quotes: int
    duplicate_events: int
    duplicate_observations: int
    out_of_order_observations: int
    watermark: datetime | None
    next_retry_at: datetime | None

    @property
    def permits_decision(self) -> bool:
        return (
            self.state is CollectorState.STREAMING
            and self.quality_report is not None
            and self.quality_report.permits_decision
            and not self.reasons
        )


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
