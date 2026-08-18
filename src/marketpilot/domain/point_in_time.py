from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from marketpilot.domain.snapshot import freeze_snapshot


class PointInTimeError(ValueError):
    """Raised when point-in-time data violates an immutable ledger invariant."""


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PointInTimeError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PointInTimeRecord:
    """An immutable, canonically serialized fact as first observed by MarketPilot."""

    record_id: str
    logical_key: str
    published_at: datetime
    first_seen_at: datetime
    provider: str
    provider_version: str
    schema_version: str
    content_hash: str
    canonical_content: str

    @classmethod
    def create(
        cls,
        *,
        logical_key: str,
        published_at: datetime,
        first_seen_at: datetime,
        provider: str,
        provider_version: str,
        schema_version: str,
        content: Mapping[str, Any],
    ) -> PointInTimeRecord:
        published = _utc(published_at, "published_at")
        first_seen = _utc(first_seen_at, "first_seen_at")
        if published > first_seen:
            raise PointInTimeError("published_at must be less than or equal to first_seen_at")

        required_strings = {
            "logical_key": logical_key,
            "provider": provider,
            "provider_version": provider_version,
            "schema_version": schema_version,
        }
        for field_name, value in required_strings.items():
            if not value.strip():
                raise PointInTimeError(f"{field_name} must not be blank")

        frozen_content = freeze_snapshot(content)
        identity = freeze_snapshot(
            {
                "logical_key": logical_key,
                "published_at": published,
                "first_seen_at": first_seen,
                "provider": provider,
                "provider_version": provider_version,
                "schema_version": schema_version,
                "content_hash": frozen_content.snapshot_id,
            }
        )
        return cls(
            record_id=identity.snapshot_id,
            logical_key=logical_key,
            published_at=published,
            first_seen_at=first_seen,
            provider=provider,
            provider_version=provider_version,
            schema_version=schema_version,
            content_hash=frozen_content.snapshot_id,
            canonical_content=frozen_content.canonical_json,
        )

    def content(self) -> dict[str, Any]:
        value = json.loads(self.canonical_content)
        if not isinstance(value, dict):
            raise PointInTimeError("record content must decode to an object")
        return value

    def verify(self) -> None:
        if self.published_at > self.first_seen_at:
            raise PointInTimeError("published_at must be less than or equal to first_seen_at")
        actual_hash = freeze_snapshot(self.content()).snapshot_id
        if actual_hash != self.content_hash:
            raise PointInTimeError(f"content hash mismatch for record {self.record_id}")
        expected_id = freeze_snapshot(
            {
                "logical_key": self.logical_key,
                "published_at": self.published_at,
                "first_seen_at": self.first_seen_at,
                "provider": self.provider,
                "provider_version": self.provider_version,
                "schema_version": self.schema_version,
                "content_hash": self.content_hash,
            }
        ).snapshot_id
        if expected_id != self.record_id:
            raise PointInTimeError(f"record identity mismatch for {self.record_id}")


@dataclass(frozen=True, slots=True)
class ReplayManifestEntry:
    logical_key: str
    record_id: str
    content_hash: str


def _manifest_digest(as_of: datetime, entries: tuple[ReplayManifestEntry, ...]) -> str:
    """Canonical digest over the replay time and ordered entries."""
    return freeze_snapshot(
        {
            "as_of": as_of,
            "entries": [
                {
                    "logical_key": entry.logical_key,
                    "record_id": entry.record_id,
                    "content_hash": entry.content_hash,
                }
                for entry in entries
            ],
        }
    ).snapshot_id


@dataclass(frozen=True, slots=True)
class ReplayManifest:
    as_of: datetime
    entries: tuple[ReplayManifestEntry, ...]
    manifest_hash: str

    @classmethod
    def create(
        cls,
        *,
        as_of: datetime,
        records: tuple[PointInTimeRecord, ...],
    ) -> ReplayManifest:
        replay_time = _utc(as_of, "as_of")
        entries = tuple(
            ReplayManifestEntry(
                logical_key=record.logical_key,
                record_id=record.record_id,
                content_hash=record.content_hash,
            )
            for record in sorted(records, key=lambda item: item.logical_key)
        )
        return cls(
            as_of=replay_time,
            entries=entries,
            manifest_hash=_manifest_digest(replay_time, entries),
        )

    def verify_hash(self) -> None:
        if _manifest_digest(self.as_of, self.entries) != self.manifest_hash:
            raise PointInTimeError("replay manifest hash mismatch")
