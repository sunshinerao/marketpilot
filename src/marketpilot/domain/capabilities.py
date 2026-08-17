from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from marketpilot.domain.market import DataQuality


class CapabilityStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class CoverageConclusion(StrEnum):
    OFFERED = "OFFERED"
    NOT_OFFERED = "NOT_OFFERED"
    UNVERIFIED = "UNVERIFIED"


class CoverageFinding(BaseModel):
    """Evidence-bounded statement about provider coverage that a probe cannot pass/fail.

    Findings record structural facts (for example, an SDK exposing no index
    endpoints) or interpretation work that remains manual, so an unmet
    requirement is a recorded conclusion rather than an untracked assumption.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_id: str
    conclusion: CoverageConclusion
    evidence: str
    required_action: str


class LatencySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    samples: int = 0
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None


class CapabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str
    status: CapabilityStatus
    checked_at: datetime
    message: str
    http_status: int | None = None
    latency: LatencySummary = Field(default_factory=LatencySummary)
    field_paths: tuple[str, ...] = ()


class CapabilityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = "webull"
    report_version: str = "2"
    probed_at: datetime
    sdk_version: str
    environment: str
    configured: bool
    quality: DataQuality
    verification_status: str = "SCHEMA_ONLY"
    production_ready: bool = False
    unverified_requirements: tuple[str, ...] = (
        "ACCOUNT_ENTITLEMENT",
        "EXCHANGE_TIMESTAMP_SEMANTICS",
        "DELAYED_FLAG",
        "NBBO_AND_SIZE",
        "HISTORICAL_DEPTH_AND_LICENSE",
        "RECONNECT_AND_RATE_LIMITS",
        "SPX_VIX_VIX1D_COVERAGE",
    )
    results: tuple[CapabilityResult, ...]
    coverage_findings: tuple[CoverageFinding, ...] = ()
