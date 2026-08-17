from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from marketpilot.domain.market import DataQuality


class EntitlementStatus(StrEnum):
    VERIFIED = "VERIFIED"
    DENIED = "DENIED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class QuoteObservation:
    source: str
    instrument_id: str
    source_ts: datetime
    received_ts: datetime
    delayed: bool | None
    entitlement: EntitlementStatus
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    field_timestamps: Mapping[str, datetime] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.instrument_id.strip():
            raise ValueError("source and instrument_id are required")
        self._require_aware("source_ts", self.source_ts)
        self._require_aware("received_ts", self.received_ts)
        for name, timestamp in self.field_timestamps.items():
            if not name.strip():
                raise ValueError("field timestamp names must not be blank")
            self._require_aware(f"field_timestamps[{name}]", timestamp)
        object.__setattr__(
            self,
            "field_timestamps",
            MappingProxyType(dict(self.field_timestamps)),
        )

    @staticmethod
    def _require_aware(name: str, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    @property
    def midpoint(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal(2)


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    green_max_age: timedelta
    amber_max_age: timedelta
    max_receive_latency: timedelta
    conflict_absolute_tolerance: Decimal
    conflict_relative_tolerance: Decimal = Decimal("0")
    require_two_sources: bool = True
    required_fields: tuple[str, ...] = ("bid", "ask", "bid_size", "ask_size")

    def __post_init__(self) -> None:
        if self.green_max_age < timedelta(0):
            raise ValueError("green_max_age must not be negative")
        if self.amber_max_age < self.green_max_age:
            raise ValueError("amber_max_age must be at least green_max_age")
        if self.max_receive_latency < timedelta(0):
            raise ValueError("max_receive_latency must not be negative")
        if self.conflict_absolute_tolerance < 0 or self.conflict_relative_tolerance < 0:
            raise ValueError("conflict tolerances must not be negative")


@dataclass(frozen=True, slots=True)
class FeedQualityReport:
    status: DataQuality
    freeze: bool
    reasons: tuple[str, ...]
    stale_fields: tuple[str, ...]
    sources: tuple[str, ...]
    observed_at: datetime

    @property
    def permits_decision(self) -> bool:
        return self.status is DataQuality.GREEN and not self.freeze


class QuoteQualityEvaluator:
    def __init__(self, policy: QualityPolicy) -> None:
        self._policy = policy

    def evaluate(
        self,
        observations: Sequence[QuoteObservation],
        *,
        as_of: datetime,
    ) -> FeedQualityReport:
        self._require_aware(as_of)
        reasons: set[str] = set()
        stale_fields: set[str] = set()
        sources = tuple(sorted({item.source for item in observations}))

        if not observations:
            reasons.add("NO_SOURCES")
        if len(sources) != len(observations):
            reasons.add("DUPLICATE_SOURCE")
        if self._policy.require_two_sources and len(sources) < 2:
            reasons.add("SECOND_SOURCE_MISSING")
        instrument_ids = {item.instrument_id for item in observations}
        if len(instrument_ids) > 1:
            reasons.add("INSTRUMENT_MISMATCH")

        worst_age = timedelta(0)
        amber_age = False
        for observation in observations:
            prefix = observation.source
            if observation.entitlement is not EntitlementStatus.VERIFIED:
                reasons.add(f"{prefix}:ENTITLEMENT_{observation.entitlement.value}")
            if observation.delayed is None:
                reasons.add(f"{prefix}:DELAY_STATUS_UNKNOWN")
            elif observation.delayed:
                reasons.add(f"{prefix}:DELAYED")
            if observation.received_ts < observation.source_ts:
                reasons.add(f"{prefix}:RECEIVED_BEFORE_SOURCE")
            elif observation.received_ts - observation.source_ts > self._policy.max_receive_latency:
                reasons.add(f"{prefix}:RECEIVE_LATENCY_EXCEEDED")

            for name in self._policy.required_fields:
                if getattr(observation, name, None) is None:
                    reasons.add(f"{prefix}:MISSING_{name.upper()}")
                timestamp = observation.field_timestamps.get(name)
                if timestamp is None:
                    reasons.add(f"{prefix}:MISSING_{name.upper()}_TIMESTAMP")
                    continue
                if timestamp > observation.received_ts:
                    reasons.add(f"{prefix}:{name.upper()}_AFTER_RECEIPT")
                age = self._age(as_of, timestamp, reasons, f"{prefix}:{name}")
                worst_age = max(worst_age, age)
                if age > self._policy.green_max_age:
                    stale_fields.add(f"{prefix}:{name}")
                if self._policy.green_max_age < age <= self._policy.amber_max_age:
                    amber_age = True
                if age > self._policy.amber_max_age:
                    reasons.add(f"{prefix}:{name.upper()}_STALE")

            source_age = self._age(as_of, observation.source_ts, reasons, f"{prefix}:source_ts")
            received_age = self._age(
                as_of,
                observation.received_ts,
                reasons,
                f"{prefix}:received_ts",
            )
            worst_age = max(worst_age, source_age, received_age)
            if max(source_age, received_age) > self._policy.green_max_age:
                amber_age = True
            if max(source_age, received_age) > self._policy.amber_max_age:
                reasons.add(f"{prefix}:OBSERVATION_STALE")
            self._validate_quote(observation, reasons)

        conflict = self._has_conflict(observations)
        if conflict:
            reasons.add("DUAL_SOURCE_CONFLICT")

        # Structural, permission, delay, quote, timestamp and conflict failures are RED.
        if reasons:
            status = DataQuality.RED
        elif amber_age or worst_age > self._policy.green_max_age:
            status = DataQuality.AMBER
        else:
            status = DataQuality.GREEN
        return FeedQualityReport(
            status=status,
            freeze=conflict or status is DataQuality.RED,
            reasons=tuple(sorted(reasons)),
            stale_fields=tuple(sorted(stale_fields)),
            sources=sources,
            observed_at=as_of.astimezone(UTC),
        )

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")

    @staticmethod
    def _age(
        as_of: datetime,
        timestamp: datetime,
        reasons: set[str],
        field: str,
    ) -> timedelta:
        age = as_of - timestamp
        if age < timedelta(0):
            reasons.add(f"{field}:FUTURE_TIMESTAMP")
            return timedelta(0)
        return age

    @staticmethod
    def _validate_quote(observation: QuoteObservation, reasons: set[str]) -> None:
        values = (observation.bid, observation.ask, observation.bid_size, observation.ask_size)
        if any(value is not None and value < 0 for value in values):
            reasons.add(f"{observation.source}:NEGATIVE_QUOTE_VALUE")
        if observation.bid_size == 0 or observation.ask_size == 0:
            reasons.add(f"{observation.source}:EMPTY_QUOTE_SIZE")
        if (
            observation.bid is not None
            and observation.ask is not None
            and observation.bid > observation.ask
        ):
            reasons.add(f"{observation.source}:CROSSED_QUOTE")

    def _has_conflict(self, observations: Sequence[QuoteObservation]) -> bool:
        if len(observations) < 2:
            return False
        midpoints = [item.midpoint for item in observations]
        if any(midpoint is None for midpoint in midpoints):
            return False
        concrete = [midpoint for midpoint in midpoints if midpoint is not None]
        low, high = min(concrete), max(concrete)
        allowed = max(
            self._policy.conflict_absolute_tolerance,
            abs(low) * self._policy.conflict_relative_tolerance,
        )
        return high - low > allowed
