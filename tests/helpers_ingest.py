"""Shared helpers for ingest tests: land one fake day through the real boundary."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from marketpilot.adapters.databento import DayPull
from marketpilot.ingest.cost_ledger import CostLedger
from marketpilot.ingest.local_landing import (
    FilesystemEncryptedObjectStore,
    JsonlLandingMetadataSink,
    LocalAesGcmCipher,
    StaticLandingAuthorizer,
)
from marketpilot.ingest.pipeline import (
    LANDING_PRINCIPAL,
    LANDING_PURPOSE,
    IngestPipeline,
)
from marketpilot.ingest.pit_ledger import PitBatchLedger
from marketpilot.services.raw_landing import LicensedPayloadLandingService


class FakeGateway:
    def estimate_cost(self, pull: DayPull) -> float:
        return 0.01

    def record_count(self, pull: DayPull) -> int:
        return 10

    def download_day(self, pull: DayPull) -> bytes:
        return f"payload:{pull.logical_key}".encode()


def land_fake_day(tmp_path: Path, day: date) -> str:
    """Land one closed day through the real encrypted boundary; return its key."""

    root = tmp_path / "raw"
    landing = LicensedPayloadLandingService(
        authorizer=StaticLandingAuthorizer(frozenset({(LANDING_PRINCIPAL, LANDING_PURPOSE)})),
        cipher=LocalAesGcmCipher(root / "_keys" / "local-aesgcm-v1.key"),
        object_store=FilesystemEncryptedObjectStore(root),
        metadata_sink=JsonlLandingMetadataSink(root / "_meta" / "receipts.jsonl"),
    )
    pipeline = IngestPipeline(
        gateway=FakeGateway(),
        landing=landing,
        pit_ledger=PitBatchLedger(tmp_path / "pit" / "records.jsonl"),
        cost_ledger=CostLedger(root / "_meta" / "cost.jsonl"),
    )
    pull = DayPull(
        dataset="OPRA.PILLAR",
        schema="cbbo-1m",
        day=day,
        stype_in="parent",
        symbols=("SPXW.OPT",),
        scope="spxw-whole-chain",
    )
    plan = pipeline.build_plan([pull], ceiling_usd=25.0)
    pipeline.run(plan)
    return pull.logical_key
