from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from marketpilot.domain.governance import GovernanceError
from marketpilot.services.governance_schemas import (
    ChallengerRegistrationInput,
    ChallengerRegistrationOutput,
    ChampionOutput,
    GovernanceActionOutput,
    GovernanceApprovalInput,
    ModelVersionsOutput,
    ValidationSummaryOutput,
)
from marketpilot.services.governance_service import GovernanceService


def create_governance_router(service: GovernanceService) -> APIRouter:
    router = APIRouter(prefix="/v1/governance", tags=["governance"])

    def conflict(exc: GovernanceError) -> HTTPException:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    @router.get("/models/{model_id}/versions", response_model=ModelVersionsOutput)
    def versions(model_id: str) -> ModelVersionsOutput:
        try:
            return service.versions(model_id)
        except GovernanceError as exc:
            raise conflict(exc) from exc

    @router.get("/models/{model_id}/champion", response_model=ChampionOutput)
    def champion(model_id: str, session_id: str | None = None) -> ChampionOutput:
        try:
            return service.champion(model_id, session_id=session_id)
        except GovernanceError as exc:
            raise conflict(exc) from exc

    @router.get("/models/{model_id}/validation", response_model=ValidationSummaryOutput)
    def validation(model_id: str) -> ValidationSummaryOutput:
        try:
            return service.validation_summary(model_id)
        except GovernanceError as exc:
            raise conflict(exc) from exc

    @router.post(
        "/models/{model_id}/challengers",
        response_model=ChallengerRegistrationOutput,
    )
    def register_challenger(
        model_id: str,
        request: ChallengerRegistrationInput,
    ) -> ChallengerRegistrationOutput:
        try:
            return service.register_local_challenger_from_api(model_id, request)
        except GovernanceError as exc:
            raise conflict(exc) from exc

    @router.post("/models/{model_id}/promotions", response_model=GovernanceActionOutput)
    def promote(model_id: str, request: GovernanceApprovalInput) -> GovernanceActionOutput:
        try:
            return service.promote_local(model_id, request)
        except GovernanceError as exc:
            raise conflict(exc) from exc

    @router.post("/models/{model_id}/rollbacks", response_model=GovernanceActionOutput)
    def rollback(model_id: str, request: GovernanceApprovalInput) -> GovernanceActionOutput:
        try:
            return service.rollback_local(model_id, request)
        except GovernanceError as exc:
            raise conflict(exc) from exc

    return router


governance_service = GovernanceService()
router = create_governance_router(governance_service)
