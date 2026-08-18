from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum

from marketpilot.domain.point_in_time import (
    PointInTimeError,
    PointInTimeRecord,
    ReplayManifest,
)


class ReplayVisibility(StrEnum):
    """Which availability fact gates a record's visibility in a replay.

    OBSERVED uses `first_seen_at` (what this system actually knew) — correct for
    live-collected data. AVAILABLE uses `published_at` (when the data could have
    been known) — correct for backtests over licensed backfilled history, where
    the bulk pull time would otherwise hide everything before the pull.
    """

    OBSERVED = "OBSERVED"
    AVAILABLE = "AVAILABLE"


class PointInTimeLedger:
    """Append-only in-memory ledger contract, ready for a durable repository adapter."""

    def __init__(self) -> None:
        self._records: dict[str, PointInTimeRecord] = {}

    def append(self, record: PointInTimeRecord) -> None:
        record.verify()
        existing = self._records.get(record.record_id)
        if existing is not None and existing != record:
            raise PointInTimeError(f"immutable record collision: {record.record_id}")
        self._records[record.record_id] = record

    def get(self, record_id: str) -> PointInTimeRecord | None:
        return self._records.get(record_id)

    def records(self) -> tuple[PointInTimeRecord, ...]:
        return tuple(sorted(self._records.values(), key=lambda item: item.record_id))


class VirtualReplayClock:
    """Selects only facts that were actually available at a historical instant."""

    def __init__(self, ledger: PointInTimeLedger) -> None:
        self._ledger = ledger

    @staticmethod
    def _as_of(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise PointInTimeError("as_of must be timezone-aware")
        return value.astimezone(UTC)

    def visible_records(
        self,
        as_of: datetime,
        *,
        visibility: ReplayVisibility = ReplayVisibility.OBSERVED,
    ) -> tuple[PointInTimeRecord, ...]:
        replay_time = self._as_of(as_of)
        latest: dict[str, PointInTimeRecord] = {}
        for record in self._ledger.records():
            record.verify()
            visible_at = (
                record.first_seen_at
                if visibility is ReplayVisibility.OBSERVED
                else record.published_at
            )
            if visible_at > replay_time:
                continue
            if record.published_at > record.first_seen_at:
                raise PointInTimeError(
                    f"record {record.record_id} was seen before it was published"
                )
            current = latest.get(record.logical_key)
            if current is None or self._revision_order(record) > self._revision_order(current):
                latest[record.logical_key] = record
        return tuple(latest[key] for key in sorted(latest))

    def build_manifest(self, as_of: datetime) -> ReplayManifest:
        replay_time = self._as_of(as_of)
        return ReplayManifest.create(
            as_of=replay_time,
            records=self.visible_records(replay_time),
        )

    def replay(self, manifest: ReplayManifest) -> tuple[PointInTimeRecord, ...]:
        manifest.verify_hash()
        replay_time = self._as_of(manifest.as_of)
        expected_visible = {
            record.logical_key: record for record in self.visible_records(replay_time)
        }
        replayed: list[PointInTimeRecord] = []
        for entry in manifest.entries:
            record = self._ledger.get(entry.record_id)
            if record is None:
                raise PointInTimeError(f"manifest record is missing: {entry.record_id}")
            record.verify()
            if record.first_seen_at > replay_time:
                raise PointInTimeError(f"future record in manifest: {entry.record_id}")
            if record.content_hash != entry.content_hash:
                raise PointInTimeError(f"manifest content hash mismatch: {entry.record_id}")
            if record.logical_key != entry.logical_key:
                raise PointInTimeError(f"manifest logical key mismatch: {entry.record_id}")
            if expected_visible.get(entry.logical_key) != record:
                raise PointInTimeError(
                    f"manifest does not select the visible revision: {entry.logical_key}"
                )
            replayed.append(record)

        if {entry.logical_key for entry in manifest.entries} != set(expected_visible):
            raise PointInTimeError("manifest is not a complete point-in-time view")
        return tuple(replayed)

    @staticmethod
    def _revision_order(record: PointInTimeRecord) -> tuple[datetime, datetime, str]:
        return (record.first_seen_at, record.published_at, record.record_id)


def append_all(ledger: PointInTimeLedger, records: Iterable[PointInTimeRecord]) -> None:
    for record in records:
        ledger.append(record)
