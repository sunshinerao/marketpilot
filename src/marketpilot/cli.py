from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

from marketpilot.adapters.databento import (
    DatabentoApiError,
    DatabentoHistoricalGateway,
    DatabentoSettings,
    DayPull,
)
from marketpilot.adapters.webull import WebullCapabilityProbe, WebullSettings
from marketpilot.domain.market import DataQuality
from marketpilot.domain.readiness import ReadinessManifest, ShadowSession
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
    DayStatus,
    IngestCostCeilingExceeded,
    IngestPipeline,
)
from marketpilot.ingest.pit_ledger import PitBatchLedger
from marketpilot.services.capability_store import CapabilityReportStore
from marketpilot.services.raw_landing import LicensedPayloadLandingService
from marketpilot.services.readiness import (
    ReadinessEvidenceError,
    ShadowLedger,
    evaluate_readiness,
    load_readiness_manifest,
    save_readiness_manifest,
)
from marketpilot.services.repository import SQLiteAuditRepository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marketpilot")
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe-webull", help="Run a redacted Webull capability probe")
    probe.add_argument("--output-dir", default="data/capability-probes")
    probe.add_argument(
        "--samples",
        type=int,
        default=1,
        help="Sampled rounds per probe for latency distributions (default: 1)",
    )
    probe.add_argument(
        "--interval-seconds",
        type=float,
        default=20.0,
        help="Pause between sampled rounds (default: 20)",
    )
    audit = commands.add_parser("audit-check", help="Verify the local SQLite audit store")
    audit.add_argument(
        "--database",
        default=os.getenv("MARKETPILOT_AUDIT_DB", "data/audit/marketpilot.sqlite3"),
    )
    template = commands.add_parser(
        "readiness-template",
        help="Write a fail-closed external-evidence manifest template",
    )
    template.add_argument("--output", default="data/readiness/readiness-manifest.json")
    template.add_argument("--environment", default="production")
    readiness = commands.add_parser(
        "readiness-check",
        help="Verify external evidence and append-only shadow-session evidence",
    )
    readiness.add_argument("--manifest", default="data/readiness/readiness-manifest.json")
    readiness.add_argument("--shadow-ledger", default="data/readiness/shadow-sessions.jsonl")
    readiness.add_argument("--minimum-sessions", type=int, default=5)
    readiness.add_argument("--minimum-trading-dates", type=int, default=3)
    readiness.add_argument(
        "--code-version",
        default=os.getenv("MARKETPILOT_CODE_VERSION", "development-unpinned"),
        help="Pinned runtime artifact identity that every qualifying session must match",
    )
    shadow = commands.add_parser(
        "shadow-record",
        help="Append a redacted read-only shadow-session summary",
    )
    shadow.add_argument("--ledger", default="data/readiness/shadow-sessions.jsonl")
    shadow.add_argument("--session-file", required=True)
    for name, helptext in (
        ("ingest-plan", "Estimate a licensed history pull without downloading"),
        ("ingest-run", "Execute a licensed history pull (spends the data budget)"),
    ):
        ingest = commands.add_parser(name, help=helptext)
        ingest.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
        ingest.add_argument("--end", required=True, help="last day, YYYY-MM-DD")
        ingest.add_argument("--calendar", default="config/us-equity-calendar-v1.toml")
        ingest.add_argument("--max-cost", type=float, default=None)
        ingest.add_argument("--data-root", default="data/raw")
        ingest.add_argument("--pit-ledger", default="data/derived/pit/batch-records.jsonl")
    commands.choices["ingest-run"].add_argument(
        "--confirm-spend",
        action="store_true",
        help="explicit owner approval to spend the estimated data budget",
    )
    ingest_audit = commands.add_parser(
        "ingest-audit",
        help="Reconcile landed batches against the trading calendar",
    )
    ingest_audit.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    ingest_audit.add_argument("--end", required=True, help="last day, YYYY-MM-DD")
    ingest_audit.add_argument("--calendar", default="config/us-equity-calendar-v1.toml")
    ingest_audit.add_argument("--pit-ledger", default="data/derived/pit/batch-records.jsonl")
    ingest_audit.add_argument("--scope", default="spxw-whole-chain")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "probe-webull":
        if args.samples < 1:
            print("status=FAIL reason=--samples must be at least 1")
            return 2
        capability_report = WebullCapabilityProbe(WebullSettings()).run(
            samples=args.samples,
            interval_seconds=args.interval_seconds,
        )
        destination = CapabilityReportStore(args.output_dir).save(capability_report)
        passed = sum(item.status == "PASS" for item in capability_report.results)
        print(
            f"provider={capability_report.provider} quality={capability_report.quality.value} "
            f"passed={passed}/{len(capability_report.results)} report={destination}"
        )
        return 0 if capability_report.quality is DataQuality.GREEN else 2
    if args.command == "audit-check":
        if args.database != ":memory:" and not Path(args.database).is_file():
            print(f"database={args.database} status=FAIL reason=NOT_FOUND")
            return 2
        try:
            repository = SQLiteAuditRepository(
                args.database,
                initialize=False,
                read_only=True,
                require_stream_schema=True,
            )
            try:
                audit_report = repository.integrity_check()
            finally:
                repository.close()
        except Exception:
            print(f"database={args.database} status=FAIL reason=INTEGRITY_ERROR")
            return 2
        print(
            f"schema_version={audit_report.schema_version} "
            f"quick_check={','.join(audit_report.quick_check)} "
            f"foreign_key_violations={audit_report.foreign_key_violations} "
            f"append_only_triggers={audit_report.append_only_triggers_installed}/"
            f"{audit_report.append_only_triggers_expected} "
            f"status={'PASS' if audit_report.ok else 'FAIL'}"
        )
        return 0 if audit_report.ok else 2
    if args.command == "readiness-template":
        destination = Path(args.output)
        if destination.exists():
            print(f"manifest={destination} status=FAIL reason=ALREADY_EXISTS")
            return 2
        manifest = ReadinessManifest.unverified_template(
            generated_at=datetime.now(UTC),
            environment=args.environment,
        )
        save_readiness_manifest(destination, manifest)
        print(
            f"manifest={destination} digest={manifest.digest()} "
            "status=UNVERIFIED action=NO_TRADE execution_enabled=false"
        )
        return 2
    if args.command == "shadow-record":
        try:
            session = ShadowSession.model_validate_json(
                Path(args.session_file).read_text(encoding="utf-8")
            )
            entry = ShadowLedger(args.ledger).append(session)
        except (OSError, UnicodeError, ValidationError, ReadinessEvidenceError, ValueError):
            print(f"ledger={args.ledger} status=FAIL reason=INVALID_SHADOW_EVIDENCE")
            return 2
        print(
            f"ledger={args.ledger} sequence={entry.sequence} entry_hash={entry.entry_hash} "
            "execution_enabled=false"
        )
        return 0
    if args.command in {"ingest-plan", "ingest-run"}:
        return _ingest(args)
    if args.command == "ingest-audit":
        ingest_report = audit_window(
            PitBatchLedger(args.pit_ledger),
            load_equity_calendar(args.calendar),
            scope=args.scope,
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
        )
        print(
            json.dumps(
                {
                    "status": "PASS" if ingest_report.ok else "FAIL",
                    "scope": ingest_report.scope,
                    "expected_trading_days": ingest_report.expected_trading_days,
                    "recorded_days": ingest_report.recorded_days,
                    "missing_trading_days": [
                        day.isoformat() for day in ingest_report.missing_trading_days
                    ],
                    "corrupt_records": ingest_report.corrupt_records,
                },
                sort_keys=True,
            )
        )
        return 0 if ingest_report.ok else 2
    if args.command == "readiness-check":
        try:
            manifest = load_readiness_manifest(args.manifest)
            entries = ShadowLedger(args.shadow_ledger).load()
            report = evaluate_readiness(
                manifest,
                entries,
                evaluated_at=datetime.now(UTC),
                expected_code_version=args.code_version,
                minimum_sessions=args.minimum_sessions,
                minimum_trading_dates=args.minimum_trading_dates,
            )
        except (ReadinessEvidenceError, ValueError):
            print(
                json.dumps(
                    {
                        "status": "FAIL",
                        "action": "NO_TRADE",
                        "execution_enabled": False,
                        "reason": "READINESS_EVIDENCE_INVALID",
                    },
                    sort_keys=True,
                )
            )
            return 2
        print(report.model_dump_json(indent=2))
        return 0 if report.shadow_admission_ready else 2
    return 2


def _default_pulls(days: Sequence[date]) -> list[DayPull]:
    pulls: list[DayPull] = []
    for day in days:
        pulls.append(
            DayPull(
                dataset="OPRA.PILLAR",
                schema="cbbo-1m",
                day=day,
                stype_in="parent",
                symbols=("SPXW.OPT",),
                scope="spxw-whole-chain",
            )
        )
        pulls.append(
            DayPull(
                dataset="GLBX.MDP3",
                schema="ohlcv-1m",
                day=day,
                stype_in="continuous",
                symbols=("ES.v.0",),
                scope="es-front-month",
            )
        )
    return pulls


def _landing_service(data_root: str) -> LicensedPayloadLandingService:
    root = Path(data_root)
    return LicensedPayloadLandingService(
        authorizer=StaticLandingAuthorizer(
            frozenset({(LANDING_PRINCIPAL, LANDING_PURPOSE)})
        ),
        cipher=LocalAesGcmCipher(root / "_keys" / "local-aesgcm-v1.key"),
        object_store=FilesystemEncryptedObjectStore(root),
        metadata_sink=JsonlLandingMetadataSink(root / "_meta" / "receipts.jsonl"),
    )


def _ingest(args: argparse.Namespace) -> int:
    settings = DatabentoSettings()
    if not settings.has_credentials:
        print("status=FAIL reason=DATABENTO_API_KEY is required")
        return 2
    calendar = load_equity_calendar(args.calendar)
    days = trading_days(
        calendar,
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
    )
    if not days:
        print("status=FAIL reason=no verified trading days in window")
        return 2
    if args.command == "ingest-run" and not args.confirm_spend:
        # Refuse before even estimating: execution requires explicit owner approval.
        print("status=FAIL reason=ingest-run requires --confirm-spend (owner approval)")
        return 2
    ceiling = args.max_cost if args.max_cost is not None else settings.max_cost_usd
    pipeline = IngestPipeline(
        gateway=DatabentoHistoricalGateway(settings),
        landing=_landing_service(args.data_root),
        pit_ledger=PitBatchLedger(args.pit_ledger),
        cost_ledger=CostLedger(Path(args.data_root) / "_meta" / "cost-ledger.jsonl"),
        report_path=Path(args.data_root) / "_meta" / "pull-reports.jsonl",
    )
    try:
        plan = pipeline.build_plan(_default_pulls(days), ceiling_usd=ceiling)
    except IngestCostCeilingExceeded as exc:
        print(
            json.dumps(
                {"status": "BLOCKED", "reason": str(exc), "execution_enabled": False},
                sort_keys=True,
            )
        )
        return 2
    except DatabentoApiError as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "reason": f"provider error ({exc.case})",
                    "execution_enabled": False,
                },
                sort_keys=True,
            )
        )
        return 2
    if args.command == "ingest-plan":
        print(
            json.dumps(
                {
                    "status": "PLAN",
                    "plan_id": plan.plan_id,
                    "trading_days": len(days),
                    "batches": len(plan.items),
                    "total_estimated_usd": plan.total_estimated_usd,
                    "ceiling_usd": plan.ceiling_usd,
                },
                sort_keys=True,
            )
        )
        return 0
    report = pipeline.run(plan)
    print(
        json.dumps(
            {
                "status": "COMPLETE",
                "plan_id": report.plan_id,
                "landed": report.count(DayStatus.LANDED),
                "skipped_present": report.count(DayStatus.SKIPPED_PRESENT),
                "not_due": report.count(DayStatus.NOT_DUE),
                "gaps": report.count(DayStatus.GAP),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
