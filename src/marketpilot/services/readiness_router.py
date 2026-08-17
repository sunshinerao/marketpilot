from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter

from marketpilot.services.readiness import (
    ReadinessEvidenceError,
    ShadowLedger,
    evaluate_readiness,
    load_readiness_manifest,
)


def create_readiness_router(
    *,
    manifest_path: str | Path = "data/readiness/readiness-manifest.json",
    shadow_ledger_path: str | Path = "data/readiness/shadow-sessions.jsonl",
    minimum_sessions: int = 5,
    minimum_trading_dates: int = 3,
    expected_code_version: str | None = None,
) -> APIRouter:
    runtime_code_version = expected_code_version or os.getenv(
        "MARKETPILOT_CODE_VERSION", "development-unpinned"
    )
    router = APIRouter(prefix="/v1/readiness", tags=["readiness"])

    @router.get("/shadow-admission")
    def shadow_admission() -> dict[str, object]:
        try:
            manifest = load_readiness_manifest(manifest_path)
            entries = ShadowLedger(shadow_ledger_path).load()
            report = evaluate_readiness(
                manifest,
                entries,
                evaluated_at=datetime.now(UTC),
                expected_code_version=runtime_code_version,
                minimum_sessions=minimum_sessions,
                minimum_trading_dates=minimum_trading_dates,
            )
        except (ReadinessEvidenceError, ValueError):
            return {
                "status": "NOT_CONFIGURED",
                "evidence_complete": False,
                "shadow_admission_ready": False,
                "production_ready": False,
                "execution_enabled": False,
                "action": "NO_TRADE",
                "manual_webull_execution_only": True,
                "blockers": ["READINESS_EVIDENCE_MISSING_OR_INVALID"],
            }
        payload = report.model_dump(mode="json")
        payload["status"] = "PASS" if report.shadow_admission_ready else "BLOCKED"
        return payload

    return router
