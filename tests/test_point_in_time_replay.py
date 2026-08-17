from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from marketpilot.domain.point_in_time import (
    PointInTimeError,
    PointInTimeRecord,
    ReplayManifest,
    ReplayManifestEntry,
)
from marketpilot.services.replay import PointInTimeLedger, VirtualReplayClock, append_all

BASE = datetime(2026, 8, 17, 12, 30, tzinfo=UTC)


def record(
    value: float,
    *,
    logical_key: str = "macro:cpi:2026-08",
    published_offset: int = 0,
    first_seen_offset: int = 1,
) -> PointInTimeRecord:
    return PointInTimeRecord.create(
        logical_key=logical_key,
        published_at=BASE + timedelta(seconds=published_offset),
        first_seen_at=BASE + timedelta(seconds=first_seen_offset),
        provider="official-source",
        provider_version="2026-08-17",
        schema_version="macro-event-v1",
        content={"value": value, "period": "2026-08"},
    )


def test_record_requires_publication_before_first_seen_and_versions() -> None:
    with pytest.raises(PointInTimeError, match="published_at"):
        record(2.7, published_offset=5, first_seen_offset=1)
    with pytest.raises(PointInTimeError, match="provider_version"):
        PointInTimeRecord.create(
            logical_key="quote:ESU6",
            published_at=BASE,
            first_seen_at=BASE,
            provider="webull",
            provider_version=" ",
            schema_version="quote-v1",
            content={"bid": 6400.0},
        )


def test_content_and_identity_are_canonical_and_tamper_evident() -> None:
    first = record(2.7)
    second = PointInTimeRecord.create(
        logical_key=first.logical_key,
        published_at=first.published_at,
        first_seen_at=first.first_seen_at,
        provider=first.provider,
        provider_version=first.provider_version,
        schema_version=first.schema_version,
        content={"period": "2026-08", "value": 2.7},
    )
    assert first == second

    tampered = replace(first, canonical_content='{"period":"2026-08","value":9.9}')
    with pytest.raises(PointInTimeError, match="content hash mismatch"):
        tampered.verify()


def test_revision_is_invisible_until_its_own_first_seen_time() -> None:
    original = record(2.7, first_seen_offset=1)
    revision = record(2.6, published_offset=120, first_seen_offset=180)
    ledger = PointInTimeLedger()
    append_all(ledger, (revision, original))
    clock = VirtualReplayClock(ledger)

    before_revision = clock.visible_records(BASE + timedelta(seconds=179))
    after_revision = clock.visible_records(BASE + timedelta(seconds=180))

    assert before_revision == (original,)
    assert after_revision == (revision,)


def test_manifest_is_deterministic_and_replays_exact_visible_view() -> None:
    cpi = record(2.7)
    es = record(
        6400.25,
        logical_key="quote:ESU6",
        published_offset=2,
        first_seen_offset=3,
    )
    first_ledger = PointInTimeLedger()
    second_ledger = PointInTimeLedger()
    append_all(first_ledger, (cpi, es))
    append_all(second_ledger, (es, cpi))
    as_of = BASE + timedelta(seconds=5)

    first_manifest = VirtualReplayClock(first_ledger).build_manifest(as_of)
    second_manifest = VirtualReplayClock(second_ledger).build_manifest(as_of)

    assert first_manifest == second_manifest
    assert VirtualReplayClock(first_ledger).replay(first_manifest) == (cpi, es)


def test_replay_rejects_future_leakage_even_with_rehashed_manifest() -> None:
    visible = record(2.7, first_seen_offset=1)
    future = record(2.6, published_offset=120, first_seen_offset=180)
    ledger = PointInTimeLedger()
    append_all(ledger, (visible, future))
    clock = VirtualReplayClock(ledger)
    as_of = BASE + timedelta(seconds=60)
    valid = clock.build_manifest(as_of)
    future_entry = ReplayManifestEntry(
        logical_key=future.logical_key,
        record_id=future.record_id,
        content_hash=future.content_hash,
    )
    forged = ReplayManifest.create(as_of=as_of, records=(future,))
    assert forged.entries == (future_entry,)

    with pytest.raises(PointInTimeError, match="future record"):
        clock.replay(forged)
    assert clock.replay(valid) == (visible,)


def test_replay_rejects_manifest_tampering_and_incomplete_views() -> None:
    cpi = record(2.7)
    es = record(6400.25, logical_key="quote:ESU6")
    ledger = PointInTimeLedger()
    append_all(ledger, (cpi, es))
    clock = VirtualReplayClock(ledger)
    manifest = clock.build_manifest(BASE + timedelta(seconds=5))

    with pytest.raises(PointInTimeError, match="manifest hash mismatch"):
        clock.replay(replace(manifest, entries=manifest.entries[:1]))

    incomplete = ReplayManifest.create(as_of=manifest.as_of, records=(cpi,))
    with pytest.raises(PointInTimeError, match="not a complete"):
        clock.replay(incomplete)
