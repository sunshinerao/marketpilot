from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from marketpilot.adapters.webull import WebullCapabilityProbe, WebullSettings
from marketpilot.domain.market import DataQuality
from marketpilot.domain.readiness import ReadinessManifest, ShadowSession
from marketpilot.services.capability_store import CapabilityReportStore
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


if __name__ == "__main__":
    raise SystemExit(main())
