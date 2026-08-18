from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from marketpilot.domain.point_in_time import (
    PointInTimeError,
    PointInTimeRecord,
    ReplayManifest,
)
from marketpilot.services.replay import (
    PointInTimeLedger,
    ReplayVisibility,
    VirtualReplayClock,
)


class PitBatchLedger:
    """Append-only JSONL ledger of batch-level point-in-time records."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def append(self, record: PointInTimeRecord) -> None:
        record.verify()
        existing = self.find(record.logical_key)
        if existing is not None and existing.record_id != record.record_id:
            raise PointInTimeError(
                f"conflicting batch record for {record.logical_key}: immutable"
            )
        if existing is not None:
            return  # identical re-append is idempotent
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "record_id": record.record_id,
                        "logical_key": record.logical_key,
                        "published_at": record.published_at.isoformat(),
                        "first_seen_at": record.first_seen_at.isoformat(),
                        "provider": record.provider,
                        "provider_version": record.provider_version,
                        "schema_version": record.schema_version,
                        "content_hash": record.content_hash,
                        "canonical_content": record.canonical_content,
                    },
                    sort_keys=True,
                )
                + "\n"
            )

    def load(self) -> tuple[PointInTimeRecord, ...]:
        if not self._path.exists():
            return ()
        records: list[PointInTimeRecord] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            record = PointInTimeRecord(
                record_id=str(raw["record_id"]),
                logical_key=str(raw["logical_key"]),
                published_at=datetime.fromisoformat(raw["published_at"]),
                first_seen_at=datetime.fromisoformat(raw["first_seen_at"]),
                provider=str(raw["provider"]),
                provider_version=str(raw["provider_version"]),
                schema_version=str(raw["schema_version"]),
                content_hash=str(raw["content_hash"]),
                canonical_content=str(raw["canonical_content"]),
            )
            record.verify()
            records.append(record)
        return tuple(records)

    def find(self, logical_key: str) -> PointInTimeRecord | None:
        for record in self.load():
            if record.logical_key == logical_key:
                return record
        return None

    def emit_manifest(
        self,
        as_of: datetime,
        *,
        visibility: ReplayVisibility = ReplayVisibility.AVAILABLE,
    ) -> ReplayManifest:
        """Backtest manifests default to AVAILABLE: bulk pull time must not hide
        history that was already published by the provider at the as-of instant."""

        ledger = PointInTimeLedger()
        for record in self.load():
            ledger.append(record)
        visible = VirtualReplayClock(ledger).visible_records(as_of, visibility=visibility)
        return ReplayManifest.create(as_of=as_of, records=visible)
