from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from marketpilot.domain.events import EventKind, EventRecord, EventSeverity
from marketpilot.domain.point_in_time import PointInTimeRecord, ReplayManifest
from marketpilot.events.risk_lock import RiskLockEngine
from marketpilot.services.replay import PointInTimeLedger, VirtualReplayClock
from marketpilot.services.schemas import (
    DemoScenarioOutput,
    ReplayManifestEntryOutput,
    ReplayManifestOutput,
)

DEMO_BASE = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class DemoScenarioArtifact:
    output: DemoScenarioOutput
    record: PointInTimeRecord
    manifest: ReplayManifest


def demo_scenarios() -> tuple[DemoScenarioOutput, ...]:
    return tuple(artifact.output for artifact in demo_scenario_artifacts())


def demo_scenario_artifacts() -> tuple[DemoScenarioArtifact, ...]:
    return (
        _scenario(
            scenario_id="unscheduled-shock-locked",
            title="Unscheduled cross-asset shock",
            summary="Unconfirmed evidence keeps Risk Lock active and the action at NO_TRADE.",
            event=EventRecord(
                event_id="demo-shock-001",
                kind=EventKind.MARKET_SHOCK,
                severity=EventSeverity.P0,
                source_published_at=DEMO_BASE,
                first_seen_at=DEMO_BASE + timedelta(seconds=5),
                corroborating_sources=1,
                cross_asset_confirmed=False,
            ),
            as_of=DEMO_BASE + timedelta(minutes=1),
        ),
        _scenario(
            scenario_id="scheduled-event-stable",
            title="Scheduled event after stability window",
            summary=(
                "Evidence gates clear in replay, but this unverified scenario remains "
                "non-executable and returns NO_TRADE."
            ),
            event=EventRecord(
                event_id="demo-scheduled-001",
                kind=EventKind.SCHEDULED,
                severity=EventSeverity.P1,
                scheduled_at=DEMO_BASE,
                source_published_at=DEMO_BASE,
                first_seen_at=DEMO_BASE + timedelta(seconds=2),
                confirmed_at=DEMO_BASE + timedelta(seconds=10),
                corroborating_sources=3,
                cross_asset_confirmed=True,
                cross_asset_confirmed_at=DEMO_BASE + timedelta(seconds=10),
                stable_since=DEMO_BASE + timedelta(seconds=30),
                stable_observations=4,
            ),
            as_of=DEMO_BASE + timedelta(minutes=3),
        ),
    )


def _scenario(
    *,
    scenario_id: str,
    title: str,
    summary: str,
    event: EventRecord,
    as_of: datetime,
) -> DemoScenarioArtifact:
    record = PointInTimeRecord.create(
        logical_key=f"event:{event.event_id}",
        published_at=event.source_published_at,
        first_seen_at=event.first_seen_at,
        provider="marketpilot-demo",
        provider_version="fixture-1",
        schema_version="event-v1",
        content=event.model_dump(mode="json"),
    )
    ledger = PointInTimeLedger()
    ledger.append(record)
    manifest = VirtualReplayClock(ledger).build_manifest(as_of)
    assessment = RiskLockEngine().assess(event, as_of)
    return DemoScenarioArtifact(
        output=DemoScenarioOutput(
            scenario_id=scenario_id,
            title=title,
            summary=summary,
            event=event,
            assessment=assessment,
            replay_manifest=_manifest_output(manifest),
        ),
        record=record,
        manifest=manifest,
    )


def _manifest_output(manifest: ReplayManifest) -> ReplayManifestOutput:
    return ReplayManifestOutput(
        as_of=manifest.as_of,
        manifest_hash=manifest.manifest_hash,
        entries=tuple(
            ReplayManifestEntryOutput(
                logical_key=entry.logical_key,
                record_id=entry.record_id,
                content_hash=entry.content_hash,
            )
            for entry in manifest.entries
        ),
    )
