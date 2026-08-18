from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from marketpilot.adapters.databento import DatabentoApiError, DayPull, enumerate_expiring
from marketpilot.ingest.audit import audit_window
from marketpilot.ingest.calendar import load_equity_calendar, trading_days
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
    SPXW_0DTE_SCOPE,
    DatabentoChainResolver,
    DayStatus,
    IngestCostCeilingExceeded,
    IngestPipeline,
)
from marketpilot.ingest.pit_ledger import PitBatchLedger
from marketpilot.services.raw_landing import LicensedPayloadLandingService

CALENDAR_PATH = "config/us-equity-calendar-v1.toml"
# 2026-08-14 is a Friday, 2026-08-15/16 a weekend, 2026-08-17/18 trading days.
WINDOW_START = date(2026, 8, 14)
WINDOW_END = date(2026, 8, 17)


class FakeGateway:
    def __init__(
        self,
        cost: float = 1.0,
        fail_on: set[str] | None = None,
        definitions: dict[date, bytes] | None = None,
    ) -> None:
        self._cost = cost
        self._fail_on = fail_on or set()
        self._definitions = definitions or {}
        self.downloads = 0
        self.definition_downloads = 0

    def estimate_cost(self, pull: DayPull) -> float:
        return self._cost

    def record_count(self, pull: DayPull) -> int:
        return 100

    def download_day(self, pull: DayPull) -> bytes:
        self.downloads += 1
        if pull.logical_key in self._fail_on:
            raise DatabentoApiError(500, "server_error")
        return f"payload:{pull.logical_key}".encode()

    def download_definitions(self, day: date) -> bytes:
        self.definition_downloads += 1
        if day not in self._definitions:
            raise DatabentoApiError(404, "no_definitions")
        return self._definitions[day]


def _ns(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1_000_000_000


def _osi(day: date, right: str, strike: str) -> str:
    return f"{'SPXW'.ljust(6)}{day:%y%m%d}{right}{strike}"


def definitions_csv(day: date, strikes: tuple[str, ...] = ("06000000", "06050000")) -> bytes:
    """A definition CSV with one call and one put expiring on ``day`` per strike."""

    lines = ["raw_symbol,expiration"]
    for strike in strikes:
        lines.append(f"{_osi(day, 'C', strike)},{_ns(day)}")
        lines.append(f"{_osi(day, 'P', strike)},{_ns(day)}")
    return ("\n".join(lines) + "\n").encode()


def zero_dte_pull(day: date) -> DayPull:
    return DayPull(
        dataset="OPRA.PILLAR",
        schema="cbbo-1m",
        day=day,
        stype_in="raw_symbol",
        symbols=("SPXW.OPT",),
        scope=SPXW_0DTE_SCOPE,
    )


def spec_pull(day: date, scope: str = "spxw-whole-chain") -> DayPull:
    return DayPull(
        dataset="OPRA.PILLAR",
        schema="cbbo-1m",
        day=day,
        stype_in="parent",
        symbols=("SPXW.OPT",),
        scope=scope,
    )


def build_pipeline(tmp_path: Path, gateway: FakeGateway) -> IngestPipeline:
    root = tmp_path / "raw"
    landing = LicensedPayloadLandingService(
        authorizer=StaticLandingAuthorizer(frozenset({(LANDING_PRINCIPAL, LANDING_PURPOSE)})),
        cipher=LocalAesGcmCipher(root / "_keys" / "k.key"),
        object_store=FilesystemEncryptedObjectStore(root),
        metadata_sink=JsonlLandingMetadataSink(root / "_meta" / "receipts.jsonl"),
    )
    return IngestPipeline(
        gateway=gateway,
        landing=landing,
        pit_ledger=PitBatchLedger(tmp_path / "pit" / "records.jsonl"),
        cost_ledger=CostLedger(tmp_path / "raw" / "_meta" / "cost.jsonl"),
    )


def window_days() -> tuple[date, ...]:
    return trading_days(load_equity_calendar(CALENDAR_PATH), WINDOW_START, WINDOW_END)


def test_calendar_expands_only_verified_trading_days() -> None:
    days = window_days()
    assert days == (date(2026, 8, 14), date(2026, 8, 17))


def test_cost_ceiling_blocks_and_records_the_decision(tmp_path: Path) -> None:
    gateway = FakeGateway(cost=100.0)
    pipeline = build_pipeline(tmp_path, gateway)

    with pytest.raises(IngestCostCeilingExceeded):
        pipeline.build_plan([spec_pull(day) for day in window_days()], ceiling_usd=25.0)

    entries = CostLedger(tmp_path / "raw" / "_meta" / "cost.jsonl").load()
    assert entries[-1].decision == "BLOCKED"
    assert entries[-1].estimated_usd == 200.0


def test_run_lands_records_and_is_idempotent(tmp_path: Path) -> None:
    gateway = FakeGateway()
    pipeline = build_pipeline(tmp_path, gateway)
    pulls = [spec_pull(day) for day in window_days()]
    plan = pipeline.build_plan(pulls, ceiling_usd=25.0)

    report = pipeline.run(plan)
    assert report.count(DayStatus.LANDED) == 2
    assert gateway.downloads == 2

    ledger = PitBatchLedger(tmp_path / "pit" / "records.jsonl")
    records = ledger.load()
    assert len(records) == 2
    content = records[0].content()
    assert content["dataset"] == "OPRA.PILLAR"
    assert content["record_count"] == 100
    assert content["plaintext_sha256"]

    # Receipts landed without payload material.
    receipts = (tmp_path / "raw" / "_meta" / "receipts.jsonl").read_text()
    assert "payload:OPRA" not in receipts

    # Re-run: everything is already present; nothing downloads twice.
    second = pipeline.run(pipeline.build_plan(pulls, ceiling_usd=25.0))
    assert second.count(DayStatus.SKIPPED_PRESENT) == 2
    assert gateway.downloads == 2


def test_same_day_pull_is_not_due(tmp_path: Path) -> None:
    gateway = FakeGateway()
    pipeline = build_pipeline(tmp_path, gateway)
    today = datetime.now(UTC).date()
    plan = pipeline.build_plan([spec_pull(today)], ceiling_usd=25.0)

    report = pipeline.run(plan)
    assert report.count(DayStatus.NOT_DUE) == 1
    assert gateway.downloads == 0


def test_failed_download_becomes_a_gap_and_run_continues(tmp_path: Path) -> None:
    days = window_days()
    failing = spec_pull(days[0])
    gateway = FakeGateway(fail_on={failing.logical_key})
    pipeline = build_pipeline(tmp_path, gateway)
    plan = pipeline.build_plan([spec_pull(day) for day in days], ceiling_usd=25.0)

    report = pipeline.run(plan)
    assert report.count(DayStatus.GAP) == 1
    assert report.count(DayStatus.LANDED) == 1

    calendar = load_equity_calendar(CALENDAR_PATH)
    audit = audit_window(
        PitBatchLedger(tmp_path / "pit" / "records.jsonl"),
        calendar,
        scope="spxw-whole-chain",
        start=WINDOW_START,
        end=WINDOW_END,
    )
    assert audit.missing_trading_days == (days[0],)
    assert not audit.ok


def test_run_report_persists_outcomes(tmp_path: Path) -> None:
    import json

    gateway = FakeGateway()
    root = tmp_path / "raw"
    report_path = root / "_meta" / "pull-reports.jsonl"
    landing = LicensedPayloadLandingService(
        authorizer=StaticLandingAuthorizer(frozenset({(LANDING_PRINCIPAL, LANDING_PURPOSE)})),
        cipher=LocalAesGcmCipher(root / "_keys" / "k.key"),
        object_store=FilesystemEncryptedObjectStore(root),
        metadata_sink=JsonlLandingMetadataSink(root / "_meta" / "receipts.jsonl"),
    )
    pipeline = IngestPipeline(
        gateway=gateway,
        landing=landing,
        pit_ledger=PitBatchLedger(tmp_path / "pit" / "records.jsonl"),
        cost_ledger=CostLedger(root / "_meta" / "cost.jsonl"),
        report_path=report_path,
    )
    plan = pipeline.build_plan([spec_pull(day) for day in window_days()], ceiling_usd=25.0)
    pipeline.run(plan)

    lines = report_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["plan_id"] == plan.plan_id
    assert all(outcome["status"] == "LANDED" for outcome in record["outcomes"])


def test_manifest_covers_only_records_visible_as_of(tmp_path: Path) -> None:
    gateway = FakeGateway()
    pipeline = build_pipeline(tmp_path, gateway)
    plan = pipeline.build_plan([spec_pull(day) for day in window_days()], ceiling_usd=25.0)
    pipeline.run(plan)

    ledger = PitBatchLedger(tmp_path / "pit" / "records.jsonl")
    early = ledger.emit_manifest(datetime(2026, 8, 15, 12, 0, tzinfo=UTC))
    late = ledger.emit_manifest(datetime(2026, 8, 19, 12, 0, tzinfo=UTC))
    # 2026-08-14's batch publishes at Friday 23:59 ET; 2026-08-17's only later.
    assert len(early.entries) == 1
    assert len(late.entries) == 2
    early.verify_hash()
    late.verify_hash()


def test_local_cipher_roundtrip_and_redacted_repr(tmp_path: Path) -> None:
    cipher = LocalAesGcmCipher(tmp_path / "key.bin")
    envelope = cipher.encrypt(b"licensed-payload", associated_data=b"ad")

    assert cipher.decrypt(envelope, associated_data=b"ad") == b"licensed-payload"
    assert "licensed-payload" not in repr(envelope)
    assert envelope.key_id == "local-aesgcm-v1"


def test_0dte_plan_resolves_chain_and_lands_definitions(tmp_path: Path) -> None:
    days = window_days()
    gateway = FakeGateway(definitions={day: definitions_csv(day) for day in days})
    pipeline = build_pipeline(tmp_path, gateway)

    plan = pipeline.build_plan(
        [zero_dte_pull(day) for day in days],
        ceiling_usd=25.0,
        chain_resolver=DatabentoChainResolver(gateway),
    )

    assert len(plan.items) == 2
    for item, day in zip(plan.items, days, strict=True):
        assert item.pull.scope == SPXW_0DTE_SCOPE
        assert item.pull.stype_in == "raw_symbol"
        expected = enumerate_expiring(definitions_csv(day), day)
        assert item.pull.symbols == expected
        assert all(len(symbol) == 21 for symbol in item.pull.symbols)
        assert item.estimated_usd == 1.0
    # Total = cbbo estimates + one definition estimate per resolved day.
    assert plan.total_estimated_usd == 4.0
    assert plan.empty_chain_outcomes == ()

    # The definition CSVs used for enumeration landed as their own PIT batches.
    records = PitBatchLedger(tmp_path / "pit" / "records.jsonl").load()
    definition_records = [r for r in records if "/definition/" in r.logical_key]
    assert len(definition_records) == 2
    content = definition_records[0].content()
    assert content["scope"] == "spxw-definitions"
    assert content["schema"] == "definition"
    assert content["record_count"] == 4
    assert definition_records[0].schema_version == "definition"
    receipts = (tmp_path / "raw" / "_meta" / "receipts.jsonl").read_text()
    assert "text/csv" in receipts
    assert "SPXW" not in receipts  # padded raw symbols never appear in receipts

    report = pipeline.run(plan)
    assert report.count(DayStatus.LANDED) == 2
    assert gateway.downloads == 2
    assert gateway.definition_downloads == 2


def test_0dte_empty_chain_day_produces_empty_chain_outcome(tmp_path: Path) -> None:
    day = window_days()[0]
    other = day + timedelta(days=7)
    # Definitions exist but nothing expires on the requested day.
    csv_bytes = f"raw_symbol,expiration\n{_osi(other, 'C', '06000000')},{_ns(other)}\n".encode()
    gateway = FakeGateway(definitions={day: csv_bytes})
    pipeline = build_pipeline(tmp_path, gateway)

    plan = pipeline.build_plan(
        [zero_dte_pull(day)],
        ceiling_usd=25.0,
        chain_resolver=DatabentoChainResolver(gateway),
    )

    assert plan.items == ()
    assert len(plan.empty_chain_outcomes) == 1
    outcome = plan.empty_chain_outcomes[0]
    assert outcome.status is DayStatus.EMPTY_CHAIN
    assert outcome.logical_key == f"OPRA.PILLAR/cbbo-1m/{SPXW_0DTE_SCOPE}/{day.isoformat()}"
    # Only the enumeration estimate counts; no cbbo estimate, no download.
    assert plan.total_estimated_usd == 1.0

    report = pipeline.run(plan)
    assert report.count(DayStatus.EMPTY_CHAIN) == 1
    assert report.count(DayStatus.LANDED) == 0
    assert gateway.downloads == 0
    # The enumeration itself is still auditable.
    ledger = PitBatchLedger(tmp_path / "pit" / "records.jsonl")
    assert ledger.find(f"OPRA.PILLAR/definition/spxw-definitions/{day.isoformat()}") is not None


def test_0dte_pull_without_resolver_is_rejected(tmp_path: Path) -> None:
    pipeline = build_pipeline(tmp_path, FakeGateway())
    with pytest.raises(ValueError, match="chain resolver"):
        pipeline.build_plan([zero_dte_pull(window_days()[0])], ceiling_usd=25.0)


def test_chain_resolver_caches_definitions_per_day(tmp_path: Path) -> None:
    day = window_days()[0]
    gateway = FakeGateway(definitions={day: definitions_csv(day)})
    resolver = DatabentoChainResolver(gateway)

    first = resolver.resolve(day)
    second = resolver.resolve(day)
    payload = resolver.definitions_payload(day)

    assert first == second
    assert payload == definitions_csv(day)
    assert gateway.definition_downloads == 1


def test_0dte_rerun_skips_enumeration_for_landed_days(tmp_path: Path) -> None:
    days = window_days()
    gateway = FakeGateway(definitions={day: definitions_csv(day) for day in days})
    pipeline = build_pipeline(tmp_path, gateway)
    pipeline.run(
        pipeline.build_plan(
            [zero_dte_pull(day) for day in days],
            ceiling_usd=25.0,
            chain_resolver=DatabentoChainResolver(gateway),
        )
    )
    assert gateway.definition_downloads == 2

    # A re-run over the same window must not re-spend on enumeration.
    plan = pipeline.build_plan(
        [zero_dte_pull(day) for day in days],
        ceiling_usd=25.0,
        chain_resolver=DatabentoChainResolver(gateway),
    )
    assert gateway.definition_downloads == 2
    assert all(item.estimated_usd == 0.0 for item in plan.items)
    report = pipeline.run(plan)
    assert report.count(DayStatus.SKIPPED_PRESENT) == 2
