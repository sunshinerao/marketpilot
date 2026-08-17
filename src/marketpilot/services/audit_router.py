from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from marketpilot.domain.decision import DecisionAction
from marketpilot.services.persistence_contracts import AuditRepository
from marketpilot.services.repository import (
    AuditIntegrityReport,
    ReplayManifestMetadata,
)
from marketpilot.services.schemas import DecisionRunOutput


class DecisionHistoryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["LOCAL"] = "LOCAL"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    decisions: tuple[DecisionRunOutput, ...]


class ReplayManifestMetadataOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_hash: str
    as_of: str
    entries: tuple[dict[str, str], ...]

    @classmethod
    def from_domain(cls, value: ReplayManifestMetadata) -> ReplayManifestMetadataOutput:
        return cls(
            manifest_hash=value.manifest_hash,
            as_of=value.as_of,
            entries=value.entries,
        )


class ReplayHistoryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["LOCAL"] = "LOCAL"
    verification: Literal["UNVERIFIED"] = "UNVERIFIED"
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    manifests: tuple[ReplayManifestMetadataOutput, ...]


class AuditIntegrityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["LOCAL"] = "LOCAL"
    execution_enabled: bool = False
    action: DecisionAction = DecisionAction.NO_TRADE
    backend: Literal["sqlite", "postgresql"]
    schema_version: str
    quick_check: tuple[str, ...]
    foreign_key_violations: int
    append_only_triggers_installed: int
    append_only_triggers_expected: int
    status: Literal["PASS", "FAIL"]

    @classmethod
    def from_domain(cls, value: AuditIntegrityReport) -> AuditIntegrityOutput:
        return cls(
            backend="sqlite",
            schema_version=value.schema_version,
            quick_check=value.quick_check,
            foreign_key_violations=value.foreign_key_violations,
            append_only_triggers_installed=value.append_only_triggers_installed,
            append_only_triggers_expected=value.append_only_triggers_expected,
            status="PASS" if value.ok else "FAIL",
        )


def create_audit_router(
    repository: AuditRepository,
    *,
    backend: Literal["sqlite", "postgresql"] = "sqlite",
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["history", "audit"])

    @router.get("/history/decisions", response_model=DecisionHistoryOutput)
    def decision_history(
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> DecisionHistoryOutput:
        return DecisionHistoryOutput(decisions=repository.decisions(limit=limit))

    @router.get("/history/replay-manifests", response_model=ReplayHistoryOutput)
    def replay_history() -> ReplayHistoryOutput:
        return ReplayHistoryOutput(
            manifests=tuple(
                ReplayManifestMetadataOutput.from_domain(item)
                for item in repository.replay_manifests()
            )
        )

    @router.get("/audit/integrity", response_model=AuditIntegrityOutput)
    def audit_integrity() -> AuditIntegrityOutput:
        output = AuditIntegrityOutput.from_domain(repository.integrity_check())
        return output.model_copy(update={"backend": backend})

    return router
