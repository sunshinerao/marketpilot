from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EventKind(StrEnum):
    SCHEDULED = "SCHEDULED"
    SOCIAL = "SOCIAL"
    GEOPOLITICAL = "GEOPOLITICAL"
    DISASTER = "DISASTER"
    MARKET_SHOCK = "MARKET_SHOCK"


class EventSeverity(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class RiskLockState(StrEnum):
    LOCKED = "LOCKED"
    STABILIZING = "STABILIZING"
    CLEARED = "CLEARED"


class EventRecord(BaseModel):
    """Point-in-time event facts used by RiskPilot.

    Narrative text is deliberately absent: the risk gate consumes evidence state and
    observed market reaction, never headline sentiment or a directional label.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    kind: EventKind
    severity: EventSeverity
    scheduled_at: datetime | None = None
    source_published_at: datetime
    first_seen_at: datetime
    confirmed_at: datetime | None = None
    market_reaction_start_at: datetime | None = None
    session_close_at: datetime | None = None
    next_cash_open_at: datetime | None = None
    corroborating_sources: int = Field(default=0, ge=0)
    contradictory_evidence: bool = False
    cross_asset_confirmed: bool = False
    cross_asset_confirmed_at: datetime | None = None
    stable_since: datetime | None = None
    stable_observations: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_timeline(self) -> EventRecord:
        timestamps = {
            "scheduled_at": self.scheduled_at,
            "source_published_at": self.source_published_at,
            "first_seen_at": self.first_seen_at,
            "confirmed_at": self.confirmed_at,
            "market_reaction_start_at": self.market_reaction_start_at,
            "session_close_at": self.session_close_at,
            "next_cash_open_at": self.next_cash_open_at,
            "cross_asset_confirmed_at": self.cross_asset_confirmed_at,
            "stable_since": self.stable_since,
        }
        for name, value in timestamps.items():
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        if self.source_published_at > self.first_seen_at:
            raise ValueError("source_published_at cannot be after first_seen_at")
        if self.confirmed_at is not None and self.confirmed_at < self.first_seen_at:
            raise ValueError("confirmed_at cannot be before first_seen_at")
        if self.cross_asset_confirmed != (self.cross_asset_confirmed_at is not None):
            raise ValueError(
                "cross_asset_confirmed and cross_asset_confirmed_at must agree"
            )
        if (
            self.cross_asset_confirmed_at is not None
            and self.cross_asset_confirmed_at < self.first_seen_at
        ):
            raise ValueError("cross_asset_confirmed_at cannot be before first_seen_at")
        if (
            self.confirmed_at is not None
            and self.cross_asset_confirmed_at is not None
            and self.cross_asset_confirmed_at < self.confirmed_at
        ):
            raise ValueError("cross_asset_confirmed_at cannot be before confirmed_at")
        stability_floor = self.cross_asset_confirmed_at or self.confirmed_at or self.first_seen_at
        if self.stable_since is not None and self.stable_since < stability_floor:
            raise ValueError("stable_since cannot predate event and cross-asset confirmation")
        if (
            self.session_close_at is not None
            and self.next_cash_open_at is not None
            and self.next_cash_open_at <= self.session_close_at
        ):
            raise ValueError("next_cash_open_at must follow session_close_at")
        return self

    def normalized(self) -> EventRecord:
        updates = {
            name: value.astimezone(UTC) if value is not None else None
            for name, value in {
                "scheduled_at": self.scheduled_at,
                "source_published_at": self.source_published_at,
                "first_seen_at": self.first_seen_at,
                "confirmed_at": self.confirmed_at,
                "market_reaction_start_at": self.market_reaction_start_at,
                "session_close_at": self.session_close_at,
                "next_cash_open_at": self.next_cash_open_at,
                "cross_asset_confirmed_at": self.cross_asset_confirmed_at,
                "stable_since": self.stable_since,
            }.items()
        }
        return self.model_copy(update=updates)


class RiskLockAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    state: RiskLockState
    assessed_at: datetime
    reasons: tuple[str, ...]
    rerun_at: datetime | None = None
    next_checkpoint: str | None = None
