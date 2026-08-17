from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from marketpilot.domain.alerts import (
    AlertCandidate,
    AlertPriority,
    AlertRecord,
    AlertStatus,
)
from marketpilot.domain.attribution import (
    AttributionReviewStatus,
    AttributionSignal,
    AttributionTriggerKind,
    CandidateCause,
    CrossAssetObservation,
)
from marketpilot.domain.events import EventSeverity
from marketpilot.services.stream_attribution_router import create_stream_attribution_router
from marketpilot.services.stream_attribution_service import StreamAttributionService
from marketpilot.services.stream_attribution_store import (
    Phase4AuditConflict,
    StreamAttributionStore,
    StreamCursorError,
)

NOW = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)


def _alert(*, status: AlertStatus = AlertStatus.OPEN) -> AlertRecord:
    return AlertRecord(
        alert_id="alert-1",
        candidate=AlertCandidate(
            fingerprint="tail-risk",
            priority=AlertPriority.P0,
            direction="DOWNSIDE",
            observed_at=NOW,
            evidence=("ES_LIQUIDITY_DROP",),
            event_id="shock-1",
            snapshot_id="sha256:alert",
            model_version="model-v1",
            rules_version="rules-v1",
            action="NO_TRADE",
            rerun_trigger="T+2m",
        ),
        created_at=NOW,
        status=status,
    )


def _signal(
    kind: AttributionTriggerKind = AttributionTriggerKind.MAJOR_EVENT,
) -> AttributionSignal:
    return AttributionSignal(
        signal_id=f"signal-{kind.value.lower()}",
        kind=kind,
        severity=EventSeverity.P0,
        observed_as_of=NOW + timedelta(minutes=2),
        first_seen_at=NOW + timedelta(seconds=30),
        # A negative lag is intentionally retained as evidence that price moved first.
        market_reaction_start_at=NOW,
        snapshot_id="sha256:trigger",
        replay_manifest_hash="sha256:manifest",
        candidates=(
            CandidateCause(
                cause_id="cause-1",
                summary="Scheduled macro release",
                source_published_at=NOW + timedelta(seconds=20),
                first_seen_at=NOW + timedelta(seconds=25),
                confidence=0.8,
                evidence_refs=("pit:event:1",),
            ),
        ),
        cross_asset_observations=(
            CrossAssetObservation(
                asset="ES",
                observed_at=NOW + timedelta(seconds=5),
                move_bps=-35,
                coherent=True,
                snapshot_id="sha256:es",
            ),
            CrossAssetObservation(
                asset="VIX",
                observed_at=NOW + timedelta(seconds=6),
                move_bps=80,
                coherent=False,
                snapshot_id="sha256:vix",
            ),
        ),
    )


async def _take(iterator: AsyncIterator[str], count: int) -> list[str]:
    frames: list[str] = []
    async for frame in iterator:
        frames.append(frame)
        if len(frames) == count:
            break
    return frames


def test_stream_reconnect_deduplicates_and_projects_acknowledged_state() -> None:
    source = [_alert()]
    store = StreamAttributionStore()
    service = StreamAttributionService(store, lambda: tuple(source), clock=lambda: NOW)

    first = asyncio.run(
        _take(
            service.stream_frames(
                last_event_id=None,
                connection_id="connection-1",
                max_frames=1,
            ),
            1,
        )
    )
    assert first[0].startswith("id: 1\nevent: alert_state\n")
    assert '"run_mode":"SCENARIO"' in first[0]
    assert '"scope":"LOCAL"' in first[0]
    assert '"action":"NO_TRADE"' in first[0]
    assert store.deliveries("1")[0].outcome == "ATTEMPTED"

    service.sync_alerts()
    assert len(store.stream_events_after(None)) == 1

    source[0] = _alert(status=AlertStatus.ACKNOWLEDGED)
    resumed = asyncio.run(
        _take(
            service.stream_frames(
                last_event_id="1",
                connection_id="connection-2",
                max_frames=1,
            ),
            1,
        )
    )
    assert resumed[0].startswith("id: 2\n")
    assert '"status":"ACKNOWLEDGED"' in resumed[0]
    assert len(store.deliveries()) == 2
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute(  # noqa: SLF001 - verifies the database invariant.
            "UPDATE stream_deliveries SET payload_json = '{}' WHERE stream_event_id = 1"
        )


def test_heartbeat_is_a_comment_and_not_claimed_as_alert_delivery() -> None:
    store = StreamAttributionStore()
    service = StreamAttributionService(store, lambda: (), clock=lambda: NOW)

    frames = asyncio.run(
        _take(
            service.stream_frames(
                last_event_id=None,
                connection_id="idle",
                heartbeat_seconds=0.001,
                poll_seconds=0.001,
                max_frames=1,
            ),
            1,
        )
    )

    assert frames[0].startswith(": heartbeat ")
    assert store.deliveries() == ()


def test_delivery_audit_failure_does_not_advance_cursor_and_reconnect_replays() -> None:
    failure_enabled = True

    def fail_delivery(operation: str) -> None:
        if failure_enabled and operation == "append_delivery":
            raise RuntimeError("injected delivery audit failure")

    store = StreamAttributionStore(failure_injector=fail_delivery)
    service = StreamAttributionService(store, lambda: (_alert(),), clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="injected"):
        asyncio.run(
            _take(
                service.stream_frames(
                    last_event_id=None,
                    connection_id="failed",
                    max_frames=1,
                ),
                1,
            )
        )
    assert store.deliveries() == ()
    assert [event.event_id for event in store.stream_events_after(None)] == ["1"]

    failure_enabled = False
    replayed = asyncio.run(
        _take(
            service.stream_frames(
                last_event_id=None,
                connection_id="retry",
                max_frames=1,
            ),
            1,
        )
    )
    assert replayed[0].startswith("id: 1\n")
    assert store.deliveries()[0].outcome == "ATTEMPTED"


def test_disconnect_before_attempt_leaves_event_replayable() -> None:
    async def disconnected() -> bool:
        return True

    store = StreamAttributionStore()
    service = StreamAttributionService(store, lambda: (_alert(),), clock=lambda: NOW)
    frames = asyncio.run(
        _take(
            service.stream_frames(
                last_event_id=None,
                connection_id="gone",
                disconnected=disconnected,
            ),
            1,
        )
    )
    assert frames == []
    assert store.deliveries() == ()
    assert [event.event_id for event in store.stream_events_after(None)] == ["1"]


def test_cursor_rejects_noncanonical_and_future_ids() -> None:
    store = StreamAttributionStore()
    service = StreamAttributionService(store, lambda: (_alert(),), clock=lambda: NOW)
    service.sync_alerts()

    with pytest.raises(StreamCursorError):
        store.validate_cursor("01")
    with pytest.raises(StreamCursorError):
        store.validate_cursor("2")


@pytest.mark.parametrize(
    "kind",
    [AttributionTriggerKind.MAJOR_EVENT, AttributionTriggerKind.ABNORMAL_MOVE],
)
def test_major_event_and_abnormal_move_create_idempotent_reverse_attribution(
    kind: AttributionTriggerKind,
) -> None:
    store = StreamAttributionStore()
    service = StreamAttributionService(store, lambda: (), clock=lambda: NOW)
    signal = _signal(kind)

    task = service.create_attribution_task(signal)
    repeated = service.create_attribution_task(signal)

    assert repeated == task
    assert task.reaction_lag_seconds == -30
    assert task.reaction_timing_interpretation == "MARKET_PRECEDED_SIGNAL"
    assert task.cross_asset_coherence == "MIXED"
    assert task.confidence == 0.68
    assert task.review_status == "OPEN"
    assert task.run_mode == "SCENARIO"
    assert task.scope == "LOCAL"
    assert task.verification == "UNVERIFIED"
    assert task.action == "NO_TRADE"
    assert task.execution_enabled is False
    assert task.counterfactual_replay_link.endswith("/counterfactual-replay")
    assert len(service.tasks()) == 1


def test_point_in_time_constraints_reject_future_attribution_evidence() -> None:
    signal = _signal()
    payload = signal.model_dump()
    payload["candidates"] = (
        signal.candidates[0].model_copy(
            update={"first_seen_at": payload["observed_as_of"] + timedelta(seconds=1)}
        ),
    )

    with pytest.raises(ValidationError, match="future first_seen_at"):
        AttributionSignal.model_validate(payload)


def test_review_is_append_only_and_counterfactual_is_non_executable() -> None:
    store = StreamAttributionStore()
    service = StreamAttributionService(store, lambda: (), clock=lambda: NOW)
    task = service.create_attribution_task(_signal())

    review, updated = service.review(
        task_id=task.task_id,
        status=AttributionReviewStatus.CONFIRMED,
        reviewer="operator",
        reviewed_at=NOW + timedelta(minutes=3),
        note="Retain after point-in-time review",
        retain_as_reusable_sample=True,
    )

    assert updated.review_status == "CONFIRMED"
    assert updated.retained_as_reusable_sample is True
    assert service.reviews(task.task_id) == (review,)
    replay = service.counterfactual_replay(task.task_id)
    assert replay["exclude_signal_id"] == task.signal.signal_id
    assert replay["execution_enabled"] is False
    assert replay["action"] == "NO_TRADE"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute(  # noqa: SLF001 - verifies the database invariant.
            "UPDATE attribution_tasks SET payload_json = '{}' WHERE task_id = ?",
            (task.task_id,),
        )


def test_attribution_write_failure_is_atomic() -> None:
    def fail_task(operation: str) -> None:
        if operation == "append_attribution_task":
            raise RuntimeError("injected attribution failure")

    store = StreamAttributionStore(failure_injector=fail_task)
    service = StreamAttributionService(store, lambda: (), clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="injected"):
        service.create_attribution_task(_signal())
    assert service.tasks() == ()


def test_router_exposes_sse_audit_attribution_review_and_replay() -> None:
    source = [_alert()]
    store = StreamAttributionStore()
    service = StreamAttributionService(store, lambda: tuple(source), clock=lambda: NOW)
    app = FastAPI()
    app.include_router(create_stream_attribution_router(service))
    client = TestClient(app)

    stream = client.get(
        "/v1/alerts/stream?max_frames=1",
        headers={"X-Connection-ID": "api-client"},
    )
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.text.startswith("id: 1\n")
    audit = client.get("/v1/alerts/stream/deliveries").json()
    assert audit["run_mode"] == "SCENARIO"
    assert audit["scope"] == "LOCAL"
    assert audit["deliveries"][0]["outcome"] == "ATTEMPTED"

    source[0] = _alert(status=AlertStatus.ACKNOWLEDGED)
    resumed = client.get(
        "/v1/alerts/stream?max_frames=1",
        headers={"Last-Event-ID": "1"},
    )
    assert resumed.text.startswith("id: 2\n")
    assert "ACKNOWLEDGED" in resumed.text
    assert client.get(
        "/v1/alerts/stream?max_frames=1",
        headers={"Last-Event-ID": "999"},
    ).status_code == 400

    created = client.post(
        "/v1/attribution/signals",
        json={
            "run_mode": "SCENARIO",
            "scope": "LOCAL",
            "signal": _signal().model_dump(mode="json"),
        },
    )
    assert created.status_code == 201
    task = created.json()
    assert task["action"] == "NO_TRADE"
    assert client.get("/v1/attribution/tasks").json()["verification"] == "UNVERIFIED"
    review = client.post(
        f"/v1/attribution/tasks/{task['task_id']}/reviews",
        json={
            "status": "INCONCLUSIVE",
            "reviewer": "operator",
            "reviewed_at": (NOW + timedelta(minutes=3)).isoformat(),
            "retain_as_reusable_sample": True,
        },
    )
    assert review.status_code == 200
    assert review.json()["task"]["retained_as_reusable_sample"] is True
    replay = client.get(task["counterfactual_replay_link"])
    assert replay.status_code == 200
    assert replay.json()["execution_enabled"] is False
    assert replay.json()["action"] == "NO_TRADE"

    live_rejected = client.post(
        "/v1/attribution/signals",
        json={
            "run_mode": "LIVE",
            "scope": "LOCAL",
            "signal": _signal().model_dump(mode="json"),
        },
    )
    assert live_rejected.status_code == 422
    missing_scope = client.post(
        "/v1/attribution/signals",
        json={
            "run_mode": "SCENARIO",
            "signal": _signal().model_dump(mode="json"),
        },
    )
    assert missing_scope.status_code == 422


def test_signal_identity_conflict_does_not_silently_replace_task() -> None:
    store = StreamAttributionStore()
    service = StreamAttributionService(store, lambda: (), clock=lambda: NOW)
    signal = _signal()
    service.create_attribution_task(signal)

    with pytest.raises(Phase4AuditConflict):
        service.create_attribution_task(
            signal.model_copy(update={"snapshot_id": "sha256:different"})
        )
