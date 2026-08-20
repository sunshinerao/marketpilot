from __future__ import annotations

import argparse
import json
import os
import tomllib
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
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
    SPXW_0DTE_SCOPE,
    DatabentoChainResolver,
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
        ingest.add_argument(
            "--strategy",
            choices=("0dte", "whole-chain"),
            default="0dte",
            help="SPXW selection: only contracts expiring that day (default, "
            "owner-approved) or the whole parent chain",
        )
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
    peek = commands.add_parser(
        "ingest-peek",
        help="Preview a landed batch (decrypts in memory, prints a table)",
    )
    peek.add_argument("--logical-key", required=True)
    peek.add_argument("--limit", type=int, default=10)
    peek.add_argument("--force", action="store_true", help="decode batches over 256 MB")
    peek.add_argument("--data-root", default="data/raw")
    peek.add_argument("--pit-ledger", default="data/derived/pit/batch-records.jsonl")
    labels = commands.add_parser(
        "calibrate-labels",
        help="Generate batch excursion labels from landed ES history",
    )
    labels.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    labels.add_argument("--end", required=True, help="last day, YYYY-MM-DD")
    labels.add_argument("--data-root", default="data/raw")
    labels.add_argument("--pit-ledger", default="data/derived/pit/batch-records.jsonl")
    labels.add_argument("--labels", default="data/derived/labels/excursions.jsonl")
    labels.add_argument("--calendar", default="config/us-equity-calendar-v1.toml")
    labels.add_argument(
        "--entry", default="09:45", help="entry time, ET wall clock (default 09:45)"
    )
    economics = commands.add_parser(
        "evaluate-economics",
        help="Conservative iron-condor economics: labels + tail distances + chains",
    )
    economics.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    economics.add_argument("--end", required=True, help="last day, YYYY-MM-DD")
    economics.add_argument("--labels", default="data/derived/labels/excursions.jsonl")
    economics.add_argument(
        "--distances",
        default="data/derived/tail-distances/distances.jsonl",
        help="workstream-F TailDistances JSONL (records shaped as TailDistances only)",
    )
    economics.add_argument("--data-root", default="data/raw")
    economics.add_argument("--pit-ledger", default="data/derived/pit/batch-records.jsonl")
    extract = commands.add_parser(
        "extract-features",
        help="Compute entry-time chain features for labelled days",
    )
    extract.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    extract.add_argument("--end", required=True, help="last day, YYYY-MM-DD")
    extract.add_argument(
        "--entry", default="09:45", help="entry time, ET wall clock (default 09:45)"
    )
    extract.add_argument("--data-root", default="data/raw")
    extract.add_argument("--pit-ledger", default="data/derived/pit/batch-records.jsonl")
    extract.add_argument("--labels", default="data/derived/labels/excursions.jsonl")
    extract.add_argument("--out", default="data/derived/labels/entry-features.jsonl")
    distances = commands.add_parser(
        "recommend-distances",
        help="Emit per-day out-of-sample tail distances with an expanding window",
    )
    distances.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    distances.add_argument("--end", required=True, help="last day, YYYY-MM-DD")
    distances.add_argument("--labels", default="data/derived/labels/excursions.jsonl")
    distances.add_argument(
        "--features", default="data/derived/labels/entry-features.jsonl"
    )
    distances.add_argument("--rules", default="config/rules-v1.toml")
    distances.add_argument("--out", default="data/derived/labels/distances.jsonl")
    distances.add_argument(
        "--model",
        choices=("iv-regime", "unconditional"),
        default="iv-regime",
    )
    distances.add_argument("--quantile", type=float, default=0.975)
    distances.add_argument("--min-train-days", type=int, default=60)
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
    if args.command == "ingest-peek":
        from marketpilot.ingest.peek import PeekError, load_landed_batch, preview_batch

        try:
            batch = load_landed_batch(
                data_root=args.data_root,
                pit_ledger_path=args.pit_ledger,
                logical_key=args.logical_key,
            )
            print(preview_batch(batch, limit=args.limit, force=args.force))
        except PeekError as exc:
            print(f"status=FAIL reason={exc}")
            return 2
        return 0
    if args.command == "calibrate-labels":
        from marketpilot.features.implied_spx import AnchorCloseError, load_anchor_closes
        from marketpilot.validation.excursion_batch import generate_labels

        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
        calendar_raw = tomllib.loads(Path(args.calendar).read_text(encoding="utf-8"))
        early_closes = {
            date.fromisoformat(entry["session_date"]): time.fromisoformat(entry["closes_at"])
            for entry in calendar_raw.get("early_closes", [])
        }
        try:
            # Widen the anchor fetch so the first window day can anchor on the
            # prior trading day's official close.
            anchors = load_anchor_closes(start - timedelta(days=10), end)
        except AnchorCloseError as exc:
            print(json.dumps({"status": "FAIL", "reason": str(exc)}, sort_keys=True))
            return 2
        try:
            label_report = generate_labels(
                data_root=args.data_root,
                pit_ledger_path=args.pit_ledger,
                anchors=anchors,
                start=start,
                end=end,
                labels_path=args.labels,
                early_closes=early_closes,
                entry_time_et=time.fromisoformat(args.entry),
            )
        except ValueError as exc:
            print(json.dumps({"status": "FAIL", "reason": str(exc)}, sort_keys=True))
            return 2
        print(
            json.dumps(
                {
                    "status": "OK",
                    "labels": str(label_report.labels_path),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "counts": label_report.counts(),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "evaluate-economics":
        from marketpilot.validation.condor_economics import run_economics_batch

        try:
            economics_report = run_economics_batch(
                labels_path=args.labels,
                distances_path=args.distances,
                data_root=args.data_root,
                pit_ledger_path=args.pit_ledger,
                start=date.fromisoformat(args.start),
                end=date.fromisoformat(args.end),
            )
        except (OSError, ValueError) as exc:
            print(json.dumps({"status": "FAIL", "reason": str(exc)}, sort_keys=True))
            return 2
        print(json.dumps(economics_report.to_dict(), sort_keys=True))
        return 0
    if args.command == "extract-features":
        from marketpilot.features.entry_snapshot_batch import generate_entry_features

        try:
            entry_time_et = time.fromisoformat(args.entry)
            feature_report = generate_entry_features(
                start=date.fromisoformat(args.start),
                end=date.fromisoformat(args.end),
                data_root=args.data_root,
                pit_ledger_path=args.pit_ledger,
                labels_path=args.labels,
                out_path=args.out,
                entry_time_et=entry_time_et,
            )
        except ValueError as exc:
            print(json.dumps({"status": "FAIL", "reason": str(exc)}, sort_keys=True))
            return 2
        print(
            json.dumps(
                {
                    "status": "OK",
                    "out": str(feature_report.out_path),
                    "start": feature_report.start.isoformat(),
                    "end": feature_report.end.isoformat(),
                    "entry": entry_time_et.isoformat(),
                    "counts": feature_report.counts(),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "recommend-distances":
        from marketpilot.validation.distance_recommender import recommend_walk_forward
        from marketpilot.validation.tail_model import (
            EntryFeatures,
            ExcursionLabel,
            IvRegimeTailModel,
            UnconditionalTailModel,
            load_tail_model_config,
        )

        excursion_labels = [
            ExcursionLabel.from_mapping(json.loads(line))
            for line in Path(args.labels).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        entry_features = [
            EntryFeatures.from_mapping(json.loads(line))
            for line in Path(args.features).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        config = load_tail_model_config(args.rules)
        factory = (
            (lambda: IvRegimeTailModel(config=config))
            if args.model == "iv-regime"
            else (lambda: UnconditionalTailModel(config=config))
        )
        distance_report = recommend_walk_forward(
            labels=excursion_labels,
            features=entry_features,
            model_factory=factory,
            quantile=args.quantile,
            min_train_days=args.min_train_days,
            out_path=Path(args.out),
        )
        print(json.dumps(distance_report.to_dict(), sort_keys=True))
        return 0
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


def _default_pulls(days: Sequence[date], *, strategy: str) -> list[DayPull]:
    if strategy not in {"0dte", "whole-chain"}:
        raise ValueError(f"unknown strategy {strategy!r}")
    pulls: list[DayPull] = []
    for day in days:
        if strategy == "0dte":
            pulls.append(
                DayPull(
                    dataset="OPRA.PILLAR",
                    schema="cbbo-1m",
                    day=day,
                    stype_in="raw_symbol",
                    # Placeholder; the chain resolver replaces it with the
                    # enumerated 0DTE raw symbols during plan building.
                    symbols=("SPXW.OPT",),
                    scope=SPXW_0DTE_SCOPE,
                )
            )
        else:
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
    gateway = DatabentoHistoricalGateway(settings)
    pipeline = IngestPipeline(
        gateway=gateway,
        landing=_landing_service(args.data_root),
        pit_ledger=PitBatchLedger(args.pit_ledger),
        cost_ledger=CostLedger(Path(args.data_root) / "_meta" / "cost-ledger.jsonl"),
        report_path=Path(args.data_root) / "_meta" / "pull-reports.jsonl",
    )
    resolver = DatabentoChainResolver(gateway) if args.strategy == "0dte" else None
    try:
        plan = pipeline.build_plan(
            _default_pulls(days, strategy=args.strategy),
            ceiling_usd=ceiling,
            chain_resolver=resolver,
        )
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
                    "strategy": args.strategy,
                    "trading_days": len(days),
                    "batches": len(plan.items),
                    "empty_chain_days": len(plan.empty_chain_outcomes),
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
                "empty_chain": report.count(DayStatus.EMPTY_CHAIN),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
