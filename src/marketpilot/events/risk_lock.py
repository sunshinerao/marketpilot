from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from marketpilot.domain.events import EventRecord, RiskLockAssessment, RiskLockState

EVENT_CHECKPOINTS: tuple[tuple[str, timedelta], ...] = (
    ("T-30m", timedelta(minutes=-30)),
    ("T-5m", timedelta(minutes=-5)),
    ("T+30s", timedelta(seconds=30)),
    ("T+2m", timedelta(minutes=2)),
    ("T+5m", timedelta(minutes=5)),
    ("T+15m", timedelta(minutes=15)),
    ("T+30m", timedelta(minutes=30)),
    ("T+60m", timedelta(minutes=60)),
)


@dataclass(frozen=True, slots=True)
class RiskLockPolicy:
    minimum_corroborating_sources: int = 2
    minimum_stable_observations: int = 3
    stability_window: timedelta = timedelta(minutes=2)


class RiskLockEngine:
    def __init__(self, policy: RiskLockPolicy | None = None) -> None:
        self._policy = policy or RiskLockPolicy()

    def assess(self, event: EventRecord, as_of: datetime) -> RiskLockAssessment:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        as_of = as_of.astimezone(UTC)
        event = event.normalized()
        if as_of < event.first_seen_at:
            raise ValueError("event is not visible at as_of")

        reasons: list[str] = []
        if event.contradictory_evidence:
            reasons.append("CONTRADICTORY_EVIDENCE")
        if event.confirmed_at is None or event.confirmed_at > as_of:
            reasons.append("EVENT_NOT_CONFIRMED")
        if event.corroborating_sources < self._policy.minimum_corroborating_sources:
            reasons.append("INSUFFICIENT_CORROBORATION")
        if (
            not event.cross_asset_confirmed
            or event.cross_asset_confirmed_at is None
            or event.cross_asset_confirmed_at > as_of
        ):
            reasons.append("CROSS_ASSET_NOT_CONFIRMED")

        if reasons:
            return self._assessment(event, as_of, RiskLockState.LOCKED, reasons)

        stable_until = (
            event.stable_since + self._policy.stability_window
            if event.stable_since is not None
            else None
        )
        stability_reasons: list[str] = []
        if event.stable_since is None or stable_until is None or event.stable_since > as_of:
            stability_reasons.append("STABILITY_WINDOW_NOT_STARTED")
        elif stable_until > as_of:
            stability_reasons.append("STABILITY_WINDOW_INCOMPLETE")
        if event.stable_observations < self._policy.minimum_stable_observations:
            stability_reasons.append("INSUFFICIENT_STABLE_OBSERVATIONS")

        if stability_reasons:
            return self._assessment(
                event,
                as_of,
                RiskLockState.STABILIZING,
                stability_reasons,
                rerun_at=stable_until,
            )
        return self._assessment(event, as_of, RiskLockState.CLEARED, ["EVENT_STABLE"])

    @staticmethod
    def _assessment(
        event: EventRecord,
        as_of: datetime,
        state: RiskLockState,
        reasons: list[str],
        rerun_at: datetime | None = None,
    ) -> RiskLockAssessment:
        anchor = event.scheduled_at or event.first_seen_at
        checkpoints = [(anchor + offset, name) for name, offset in EVENT_CHECKPOINTS]
        if event.session_close_at is not None:
            checkpoints.append((event.session_close_at, "SESSION_CLOSE"))
        if event.next_cash_open_at is not None:
            checkpoints.append((event.next_cash_open_at, "NEXT_CASH_OPEN"))
        next_checkpoint = next(
            (name for checkpoint_at, name in sorted(checkpoints) if checkpoint_at > as_of),
            (
                "MONITORING_COMPLETE"
                if event.next_cash_open_at is not None
                else "SESSION_BOUNDARY_UNCONFIGURED"
            ),
        )
        return RiskLockAssessment(
            event_id=event.event_id,
            state=state,
            assessed_at=as_of,
            reasons=tuple(reasons),
            rerun_at=rerun_at,
            next_checkpoint=next_checkpoint,
        )
