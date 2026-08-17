from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from marketpilot.domain.contracts import normalize_explicit_es_symbol

EvidenceId = Annotated[
    str,
    Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]


class EvidenceRequirement(StrEnum):
    WEBULL_ACCOUNT_ENTITLEMENT = "WEBULL_ACCOUNT_ENTITLEMENT"
    LICENSED_MARKET_DATA_COVERAGE = "LICENSED_MARKET_DATA_COVERAGE"
    POINT_IN_TIME_TIMESTAMP_SEMANTICS = "POINT_IN_TIME_TIMESTAMP_SEMANTICS"
    EXPIRED_SPXW_NBBO_HISTORY = "EXPIRED_SPXW_NBBO_HISTORY"
    LIVE_EVENT_SOURCE_COVERAGE = "LIVE_EVENT_SOURCE_COVERAGE"
    PRODUCTION_SECURITY_AND_RECOVERY = "PRODUCTION_SECURITY_AND_RECOVERY"
    UNTOUCHED_HOLDOUT_APPROVAL = "UNTOUCHED_HOLDOUT_APPROVAL"


REQUIRED_EXTERNAL_EVIDENCE: tuple[EvidenceRequirement, ...] = tuple(EvidenceRequirement)


class EvidenceStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class EvidenceAuthority(StrEnum):
    AUTHORIZED_PROVIDER = "AUTHORIZED_PROVIDER"
    INDEPENDENT_LICENSEE = "INDEPENDENT_LICENSEE"
    INFRASTRUCTURE_CONTROL = "INFRASTRUCTURE_CONTROL"
    APPROVAL_AUTHORITY = "APPROVAL_AUTHORITY"
    LOCAL_SIMULATION = "LOCAL_SIMULATION"


ALLOWED_EVIDENCE_AUTHORITIES: dict[EvidenceRequirement, frozenset[EvidenceAuthority]] = {
    EvidenceRequirement.WEBULL_ACCOUNT_ENTITLEMENT: frozenset(
        {EvidenceAuthority.AUTHORIZED_PROVIDER}
    ),
    EvidenceRequirement.LICENSED_MARKET_DATA_COVERAGE: frozenset(
        {EvidenceAuthority.INDEPENDENT_LICENSEE}
    ),
    EvidenceRequirement.POINT_IN_TIME_TIMESTAMP_SEMANTICS: frozenset(
        {EvidenceAuthority.AUTHORIZED_PROVIDER, EvidenceAuthority.INDEPENDENT_LICENSEE}
    ),
    EvidenceRequirement.EXPIRED_SPXW_NBBO_HISTORY: frozenset(
        {EvidenceAuthority.INDEPENDENT_LICENSEE}
    ),
    EvidenceRequirement.LIVE_EVENT_SOURCE_COVERAGE: frozenset(
        {EvidenceAuthority.INDEPENDENT_LICENSEE}
    ),
    EvidenceRequirement.PRODUCTION_SECURITY_AND_RECOVERY: frozenset(
        {EvidenceAuthority.INFRASTRUCTURE_CONTROL}
    ),
    EvidenceRequirement.UNTOUCHED_HOLDOUT_APPROVAL: frozenset(
        {EvidenceAuthority.APPROVAL_AUTHORITY}
    ),
}


class ReadinessEvidence(BaseModel):
    """Redacted evidence metadata, never credentials or licensed payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement: EvidenceRequirement
    status: EvidenceStatus = EvidenceStatus.UNVERIFIED
    authority: EvidenceAuthority = EvidenceAuthority.LOCAL_SIMULATION
    issuer: str | None = Field(default=None, max_length=120)
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    artifact_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    review_id: str | None = Field(default=None, max_length=120)
    scope: str | None = Field(default=None, max_length=500)
    redactions_confirmed: bool = False
    raw_payload_included: Literal[False] = False

    @model_validator(mode="after")
    def verified_evidence_requires_external_review(self) -> ReadinessEvidence:
        if self.status is not EvidenceStatus.VERIFIED:
            return self
        if self.authority is EvidenceAuthority.LOCAL_SIMULATION:
            raise ValueError("VERIFIED evidence cannot be based on LOCAL_SIMULATION")
        if self.authority not in ALLOWED_EVIDENCE_AUTHORITIES[self.requirement]:
            raise ValueError(
                f"{self.authority.value} cannot verify {self.requirement.value}"
            )
        required = {
            "issuer": self.issuer,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "artifact_sha256": self.artifact_sha256,
            "review_id": self.review_id,
            "scope": self.scope,
        }
        missing = sorted(name for name, value in required.items() if value is None)
        if missing:
            raise ValueError(f"VERIFIED evidence is missing: {','.join(missing)}")
        if not self.redactions_confirmed:
            raise ValueError("VERIFIED evidence must confirm redactions")
        assert self.observed_at is not None
        assert self.expires_at is not None
        if self.expires_at <= self.observed_at:
            raise ValueError("evidence expiry must follow observation time")
        return self

    @field_validator("observed_at", "expires_at")
    @classmethod
    def require_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence timestamps must be timezone-aware")
        return value.astimezone(UTC)


class ReadinessManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_version: Literal["1"] = "1"
    generated_at: datetime
    environment: str = Field(min_length=1, max_length=80)
    evidence: tuple[ReadinessEvidence, ...]
    manual_webull_execution_only: Literal[True] = True
    automated_order_submission: Literal[False] = False

    @field_validator("generated_at")
    @classmethod
    def require_aware_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def requirements_are_unique(self) -> ReadinessManifest:
        requirements = [item.requirement for item in self.evidence]
        if len(requirements) != len(set(requirements)):
            raise ValueError("readiness evidence requirements must be unique")
        if any(
            item.observed_at is not None and item.observed_at > self.generated_at
            for item in self.evidence
        ):
            raise ValueError("manifest cannot predate its evidence")
        return self

    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"

    @classmethod
    def unverified_template(cls, *, generated_at: datetime, environment: str) -> ReadinessManifest:
        return cls(
            generated_at=generated_at,
            environment=environment,
            evidence=tuple(
                ReadinessEvidence(requirement=requirement)
                for requirement in REQUIRED_EXTERNAL_EVIDENCE
            ),
        )


class ShadowSession(BaseModel):
    """One redacted, read-only session summary suitable for an append-only ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,119}$")
    trading_date: date
    started_at: datetime
    ended_at: datetime
    environment: str = Field(min_length=1, max_length=80)
    readiness_manifest_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    capability_report_ids: tuple[EvidenceId, ...] = Field(default=(), max_length=20)
    exact_es_contract: str | None = None
    code_version: str = Field(min_length=1, max_length=120)
    rules_version: str = Field(min_length=1, max_length=120)
    model_version: str = Field(min_length=1, max_length=120)
    audit_export_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    operator_review_id: str = Field(min_length=1, max_length=120)
    decision_count: int = Field(ge=0, le=100_000)
    no_trade_count: int = Field(ge=0, le=100_000)
    wait_count: int = Field(ge=0, le=100_000)
    audit_integrity_passed: bool
    source_degradation_drill_passed: bool = False
    recovery_drill_passed: bool = False
    source_degradation_evidence_sha256: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    recovery_evidence_sha256: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    incident_ids: tuple[EvidenceId, ...] = Field(default=(), max_length=100)
    execution_enabled: Literal[False] = False
    automated_orders_created: Literal[0] = 0
    raw_licensed_payloads_included: Literal[False] = False

    @field_validator("started_at", "ended_at")
    @classmethod
    def require_aware_session_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("shadow session timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("exact_es_contract")
    @classmethod
    def normalize_es_contract(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_explicit_es_symbol(value)

    @model_validator(mode="after")
    def validate_counts_and_window(self) -> ShadowSession:
        if self.ended_at <= self.started_at:
            raise ValueError("shadow session must end after it starts")
        if self.trading_date != self.started_at.astimezone(
            ZoneInfo("America/New_York")
        ).date():
            raise ValueError("trading_date must match the session start date in America/New_York")
        if self.no_trade_count + self.wait_count != self.decision_count:
            raise ValueError("every shadow decision must be NO_TRADE or WAIT")
        if self.source_degradation_drill_passed and not self.source_degradation_evidence_sha256:
            raise ValueError("a passing source-degradation drill requires evidence")
        if self.recovery_drill_passed and not self.recovery_evidence_sha256:
            raise ValueError("a passing recovery drill requires evidence")
        return self


class ShadowLedgerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    previous_hash: str = Field(pattern=r"^(GENESIS|sha256:[0-9a-f]{64})$")
    session: ShadowSession
    entry_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ShadowEvidenceReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chain_valid: bool
    ledger_head_sha256: str | None
    recorded_sessions: int
    qualifying_sessions: int
    qualifying_trading_dates: int
    rejected_time_window_sessions: int
    degradation_drill_observed: bool
    recovery_drill_observed: bool
    blockers: tuple[str, ...]


class ReadinessGateReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluated_at: datetime
    manifest_sha256: str
    evidence_complete: bool
    shadow_evidence: ShadowEvidenceReport
    shadow_admission_ready: bool
    production_ready: Literal[False] = False
    execution_enabled: Literal[False] = False
    action: Literal["NO_TRADE"] = "NO_TRADE"
    manual_webull_execution_only: Literal[True] = True
    blockers: tuple[str, ...]
