from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from marketpilot.domain.alerts import AlertRecord
from marketpilot.domain.attribution import (
    AttributionReview,
    AttributionReviewStatus,
    AttributionSignal,
    AttributionTask,
    CrossAssetCoherence,
    ReactionTimingInterpretation,
)
from marketpilot.domain.streaming import DeliveryAuditRecord
from marketpilot.services.persistence_contracts import StreamAttributionRepository

AlertsProvider = Callable[[], tuple[AlertRecord, ...]]
Clock = Callable[[], datetime]


class AttributionWorkflowError(ValueError):
    """A review attempts an invalid or unsafe state transition."""


class StreamAttributionService:
    """Coordinates local SSE projection and reverse-attribution workflows."""

    def __init__(
        self,
        store: StreamAttributionRepository,
        alerts_provider: AlertsProvider,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.store = store
        self._alerts_provider = alerts_provider
        self._clock = clock or (lambda: datetime.now(UTC))

    def sync_alerts(self) -> int:
        appended = 0
        for alert in self._alerts_provider():
            before = self.store.stream_events_after(None)
            self.store.append_alert_projection(
                projection_key=_projection_key(alert),
                alert=alert,
                recorded_at=self._clock(),
            )
            after = self.store.stream_events_after(None)
            appended += len(after) - len(before)
        return appended

    def validate_cursor(self, last_event_id: str | None) -> None:
        self.sync_alerts()
        self.store.validate_cursor(last_event_id)

    async def stream_frames(
        self,
        *,
        last_event_id: str | None,
        connection_id: str,
        heartbeat_seconds: float = 15.0,
        poll_seconds: float = 0.25,
        max_frames: int | None = None,
        disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[str]:
        """Yield resumable SSE frames; ``max_frames`` exists for deterministic local tests."""

        self.validate_cursor(last_event_id)
        cursor = last_event_id
        emitted = 0
        heartbeat_due = asyncio.get_running_loop().time() + heartbeat_seconds
        while max_frames is None or emitted < max_frames:
            if disconnected is not None and await disconnected():
                return

            self.sync_alerts()
            pending = self.store.stream_events_after(cursor)
            if pending:
                for event in pending:
                    delivery = DeliveryAuditRecord(
                        delivery_id=str(uuid4()),
                        connection_id=connection_id,
                        stream_event_id=event.event_id,
                        attempted_at=self._clock(),
                    )
                    # Audit must commit before the frame is handed to the response. On
                    # failure the cursor does not advance, so reconnect safely retries.
                    self.store.append_delivery(delivery)
                    yield _event_frame(event.event_id, event.model_dump(mode="json"))
                    cursor = event.event_id
                    emitted += 1
                    heartbeat_due = asyncio.get_running_loop().time() + heartbeat_seconds
                    if max_frames is not None and emitted >= max_frames:
                        return
                continue

            now = asyncio.get_running_loop().time()
            if now >= heartbeat_due:
                yield f": heartbeat {self._clock().astimezone(UTC).isoformat()}\n\n"
                emitted += 1
                heartbeat_due = now + heartbeat_seconds
                if max_frames is not None and emitted >= max_frames:
                    return
            await asyncio.sleep(poll_seconds)

    def create_attribution_task(self, signal: AttributionSignal) -> AttributionTask:
        digest = hashlib.sha256(signal.signal_id.encode("utf-8")).hexdigest()[:24]
        coherence = _coherence(signal)
        confidence = _confidence(signal, coherence)
        reaction_lag = (signal.market_reaction_start_at - signal.first_seen_at).total_seconds()
        task = AttributionTask(
            task_id=f"attr-{digest}",
            signal=signal,
            created_at=signal.observed_as_of.astimezone(UTC),
            reaction_lag_seconds=reaction_lag,
            reaction_timing_interpretation=_timing_interpretation(reaction_lag),
            cross_asset_coherence=coherence,
            confidence=confidence,
            counterfactual_replay_link=(
                f"/v1/attribution/tasks/attr-{digest}/counterfactual-replay"
            ),
        )
        return self.store.append_attribution_task(task)

    def tasks(self) -> tuple[AttributionTask, ...]:
        return self.store.attribution_tasks()

    def task(self, task_id: str) -> AttributionTask:
        task = self.store.get_attribution_task(task_id)
        if task is None:
            raise KeyError(f"unknown attribution task: {task_id}")
        return task

    def reviews(self, task_id: str) -> tuple[AttributionReview, ...]:
        self.task(task_id)
        return self.store.attribution_reviews(task_id)

    def review(
        self,
        *,
        task_id: str,
        status: AttributionReviewStatus,
        reviewer: str,
        reviewed_at: datetime,
        note: str | None,
        retain_as_reusable_sample: bool,
    ) -> tuple[AttributionReview, AttributionTask]:
        current = self.task(task_id)
        _validate_review_transition(current.review_status, status)
        if retain_as_reusable_sample and status not in {
            AttributionReviewStatus.CONFIRMED,
            AttributionReviewStatus.INCONCLUSIVE,
        }:
            raise AttributionWorkflowError(
                "only a completed review can retain a reusable shock sample"
            )
        review = AttributionReview(
            review_id=str(uuid4()),
            task_id=task_id,
            status=status,
            reviewer=reviewer,
            reviewed_at=reviewed_at,
            note=note,
            retain_as_reusable_sample=retain_as_reusable_sample,
        )
        self.store.append_attribution_review(review)
        return review, self.task(task_id)

    def counterfactual_replay(self, task_id: str) -> dict[str, object]:
        task = self.task(task_id)
        return {
            "task_id": task.task_id,
            "as_of": task.signal.first_seen_at,
            "snapshot_id": task.signal.snapshot_id,
            "replay_manifest_hash": task.signal.replay_manifest_hash,
            "exclude_signal_id": task.signal.signal_id,
            "purpose": "COUNTERFACTUAL_ATTRIBUTION_REPLAY",
            "execution_enabled": False,
            "action": "NO_TRADE",
        }


def _projection_key(alert: AlertRecord) -> str:
    serialized = json.dumps(
        alert.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _event_frame(event_id: str, payload: dict[str, object]) -> str:
    data = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"id: {event_id}\nevent: alert_state\ndata: {data}\n\n"


def _coherence(signal: AttributionSignal) -> CrossAssetCoherence:
    observations = signal.cross_asset_observations
    if not observations:
        return CrossAssetCoherence.UNKNOWN
    coherent = sum(observation.coherent for observation in observations)
    if coherent == len(observations):
        return CrossAssetCoherence.COHERENT
    if coherent == 0:
        return CrossAssetCoherence.DIVERGENT
    return CrossAssetCoherence.MIXED


def _confidence(signal: AttributionSignal, coherence: CrossAssetCoherence) -> float:
    if not signal.candidates:
        return 0.0
    factor = {
        CrossAssetCoherence.COHERENT: 1.0,
        CrossAssetCoherence.MIXED: 0.85,
        CrossAssetCoherence.DIVERGENT: 0.6,
        CrossAssetCoherence.UNKNOWN: 0.75,
    }[coherence]
    return round(max(candidate.confidence for candidate in signal.candidates) * factor, 4)


def _timing_interpretation(reaction_lag: float) -> ReactionTimingInterpretation:
    """A negative lag is retained as evidence that the market moved first."""

    if reaction_lag < 0:
        return ReactionTimingInterpretation.MARKET_PRECEDED_SIGNAL
    if reaction_lag > 0:
        return ReactionTimingInterpretation.SIGNAL_PRECEDED_MARKET
    return ReactionTimingInterpretation.SIMULTANEOUS


def _validate_review_transition(
    current: AttributionReviewStatus,
    requested: AttributionReviewStatus,
) -> None:
    terminal = {
        AttributionReviewStatus.CONFIRMED,
        AttributionReviewStatus.REJECTED,
        AttributionReviewStatus.INCONCLUSIVE,
    }
    if requested is AttributionReviewStatus.OPEN:
        raise AttributionWorkflowError("review status cannot transition back to OPEN")
    if current in terminal:
        raise AttributionWorkflowError(f"review is terminal: {current}")
