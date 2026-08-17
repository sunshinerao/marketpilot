from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime

from marketpilot.domain.governance import (
    ApprovalAction,
    GovernanceApproval,
    GovernanceError,
    ModelVersion,
)
from marketpilot.domain.snapshot import freeze_snapshot
from marketpilot.governance.registry import FrozenChampionRegistry
from marketpilot.models.strikepilot.model import strikepilot_artifact_hash
from marketpilot.services.governance_schemas import (
    ApprovalRunMode,
    ApprovalScope,
    CalibrationStatus,
    ChallengerRegistrationInput,
    ChallengerRegistrationOutput,
    ChampionOutput,
    GovernanceActionOutput,
    GovernanceApprovalInput,
    ModelVersionOutput,
    ModelVersionsOutput,
    NoTradeEffectOutput,
    ValidationSliceOutput,
    ValidationSummaryOutput,
)
from marketpilot.services.persistence_contracts import ChampionRegistry
from marketpilot.validation.metrics import ValidationResult, summarize_validation

BASELINE_MODEL_ID = "strikepilot_spxw_0dte_ic"
BASELINE_VERSION = "0.1.0-baseline"


class GovernanceService:
    """Local/scenario governance facade; it has no path that can enable live execution."""

    def __init__(
        self,
        registry: ChampionRegistry | None = None,
        *,
        baseline_artifact_hash: str | None = None,
    ) -> None:
        self._registry = registry or FrozenChampionRegistry()
        self._validation_results: dict[str, tuple[ValidationResult, ...]] = {}
        self._validation_report_hashes: dict[str, str] = {}
        if not self._registry.versions(BASELINE_MODEL_ID):
            baseline_identity = baseline_artifact_hash or strikepilot_artifact_hash(
                strike_increment=5,
                wing_width=5,
            )
            self._registry.register_challenger(
                ModelVersion(
                    model_id=BASELINE_MODEL_ID,
                    version=BASELINE_VERSION,
                    artifact_hash=baseline_identity,
                    data_manifest_hash=freeze_snapshot({"status": "NO_DATA_MANIFEST"}).snapshot_id,
                    trained_at=datetime(2026, 8, 15, tzinfo=UTC),
                    validation_report_hash=None,
                )
            )

    def register_local_challenger(self, model: ModelVersion) -> None:
        """Local setup hook for offline validation workflows; never deploys the model."""
        self._registry.register_challenger(model)

    def register_local_challenger_from_api(
        self,
        model_id: str,
        request: ChallengerRegistrationInput,
    ) -> ChallengerRegistrationOutput:
        if request.run_mode is not ApprovalRunMode.SCENARIO:
            raise GovernanceError("challenger registration requires SCENARIO mode")
        if request.scope is not ApprovalScope.LOCAL:
            raise GovernanceError("challenger registration is restricted to LOCAL scope")
        model = ModelVersion(
            model_id=model_id,
            version=request.version,
            artifact_hash=request.artifact_hash,
            data_manifest_hash=request.data_manifest_hash,
            trained_at=request.trained_at,
            validation_report_hash=request.validation_report_hash,
            parent_version=request.parent_version,
        )
        self._registry.register_challenger(model)
        return ChallengerRegistrationOutput(
            model=self._version_output(model, self._champion_version(model_id))
        )

    def record_local_validation(
        self,
        model_id: str,
        *,
        report_hash: str,
        results: Sequence[ValidationResult],
    ) -> None:
        if not report_hash.strip():
            raise GovernanceError("report_hash must not be blank")
        self._validation_results[model_id] = tuple(results)
        self._validation_report_hashes[model_id] = report_hash

    def versions(self, model_id: str) -> ModelVersionsOutput:
        models = self._registry.versions(model_id)
        if not models:
            raise GovernanceError(f"unknown model: {model_id}")
        champion_version = self._champion_version(model_id)
        return ModelVersionsOutput(
            model_id=model_id,
            versions=[self._version_output(model, champion_version) for model in models],
        )

    def champion(self, model_id: str, *, session_id: str | None = None) -> ChampionOutput:
        if session_id is not None and not session_id.startswith("SCENARIO:"):
            raise GovernanceError("public session freeze is restricted to SCENARIO sessions")
        model = (
            self._registry.freeze_session(model_id, session_id)
            if session_id is not None
            else self._registry.champion(model_id)
        )
        return ChampionOutput(
            model_id=model_id,
            champion=self._version_output(model, model.version),
            session_id=session_id,
            frozen_for_session=session_id is not None,
        )

    def promote_local(
        self, model_id: str, request: GovernanceApprovalInput
    ) -> GovernanceActionOutput:
        self._require_local_scenario(request)
        approval = self._approval(ApprovalAction.PROMOTE, model_id, request)
        self._registry.promote(model_id, request.target_version, approval)
        return GovernanceActionOutput(
            action=ApprovalAction.PROMOTE,
            approval_id=approval.approval_id,
            champion=self.champion(model_id),
            message="Local scenario champion changed; live execution remains disabled.",
        )

    def rollback_local(
        self, model_id: str, request: GovernanceApprovalInput
    ) -> GovernanceActionOutput:
        self._require_local_scenario(request)
        approval = self._approval(ApprovalAction.ROLLBACK, model_id, request)
        self._registry.rollback(model_id, request.target_version, approval)
        return GovernanceActionOutput(
            action=ApprovalAction.ROLLBACK,
            approval_id=approval.approval_id,
            champion=self.champion(model_id),
            message="Local scenario rollback completed; live execution remains disabled.",
        )

    def validation_summary(self, model_id: str) -> ValidationSummaryOutput:
        if not self._registry.versions(model_id):
            raise GovernanceError(f"unknown model: {model_id}")
        results = self._validation_results.get(model_id, ())
        report_hash = self._validation_report_hashes.get(model_id)
        summaries = summarize_validation(results) if results else ()
        return ValidationSummaryOutput(
            model_id=model_id,
            calibration_status=(
                CalibrationStatus.LOCAL_VALIDATION_AVAILABLE
                if report_hash is not None
                else CalibrationStatus.NOT_CALIBRATED
            ),
            report_hash=report_hash,
            slices=[
                ValidationSliceOutput(
                    strata=list(summary.strata),
                    sample_count=summary.sample_count,
                    action_counts=dict(summary.action_counts),
                    metric_means=dict(summary.metric_means),
                    no_trade_effect=NoTradeEffectOutput(**asdict(summary.no_trade_effect)),
                )
                for summary in summaries
            ],
        )

    def _version_output(
        self, model: ModelVersion, champion_version: str | None
    ) -> ModelVersionOutput:
        return ModelVersionOutput(
            model_id=model.model_id,
            version=model.version,
            artifact_hash=model.artifact_hash,
            data_manifest_hash=model.data_manifest_hash,
            trained_at=model.trained_at,
            validation_report_hash=model.validation_report_hash,
            parent_version=model.parent_version,
            is_local_champion=model.version == champion_version,
            calibration_status=(
                CalibrationStatus.LOCAL_VALIDATION_AVAILABLE
                if model.validation_report_hash is not None
                else CalibrationStatus.NOT_CALIBRATED
            ),
        )

    def _champion_version(self, model_id: str) -> str | None:
        try:
            return self._registry.champion(model_id).version
        except GovernanceError:
            return None

    @staticmethod
    def _require_local_scenario(request: GovernanceApprovalInput) -> None:
        if request.run_mode is not ApprovalRunMode.SCENARIO:
            raise GovernanceError("governance writes require SCENARIO mode")
        if request.scope is not ApprovalScope.LOCAL:
            raise GovernanceError("governance writes are restricted to LOCAL scope")

    @staticmethod
    def _approval(
        action: ApprovalAction,
        model_id: str,
        request: GovernanceApprovalInput,
    ) -> GovernanceApproval:
        return GovernanceApproval.create(
            action=action,
            model_id=model_id,
            source_version=request.source_version,
            target_version=request.target_version,
            approved_by=request.approved_by,
            approved_at=request.approved_at,
            evidence_hash=request.evidence_hash,
            note=request.note,
        )
