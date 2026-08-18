from __future__ import annotations

import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, status

from marketpilot.config import load_rules
from marketpilot.decision.gates import DecisionGateContext
from marketpilot.decision.runner import DecisionRunner
from marketpilot.domain.capabilities import CapabilityStatus
from marketpilot.domain.governance import GovernanceError
from marketpilot.domain.market import DataQuality, DataQualityReport, MarketSnapshot
from marketpilot.domain.snapshot import freeze_snapshot
from marketpilot.models.base import ModelArtifactIdentity
from marketpilot.models.registry import ModelRegistry
from marketpilot.models.strikepilot.model import StrikePilotModel
from marketpilot.services.alert_delivery_router import create_alert_delivery_router
from marketpilot.services.audit_router import create_audit_router
from marketpilot.services.auth import AuthConfig, install_auth
from marketpilot.services.capability_store import CapabilityReportStore
from marketpilot.services.collector_router import create_collector_router
from marketpilot.services.economics_router import create_economics_router
from marketpilot.services.governance_router import create_governance_router
from marketpilot.services.governance_service import GovernanceService
from marketpilot.services.operations import OperationsService
from marketpilot.services.readiness_router import create_readiness_router
from marketpilot.services.runtime_persistence import create_runtime_persistence
from marketpilot.services.schemas import (
    AlertFeedbackInput,
    AlertFeedbackListOutput,
    AlertFeedbackOutput,
    AlertListOutput,
    DecisionRunInput,
    DecisionRunMode,
    DecisionRunOutput,
    DemoScenarioOutput,
    EventAssessmentInput,
    EventAssessmentOutput,
    OverviewOutput,
)
from marketpilot.services.session_quality_router import create_session_quality_router
from marketpilot.services.state import DecisionStore
from marketpilot.services.stream_attribution_router import create_stream_attribution_router
from marketpilot.services.stream_attribution_service import StreamAttributionService
from marketpilot.services.validation_gate_router import create_validation_gate_router
from marketpilot.validation.promotion_gate import load_promotion_criteria

logger = logging.getLogger("marketpilot.services.api")

auth_config = AuthConfig.from_env(os.environ)
app = FastAPI(
    title="MarketPilot API",
    version="0.1.0",
    docs_url="/docs" if auth_config.docs_enabled else None,
    redoc_url="/redoc" if auth_config.docs_enabled else None,
    openapi_url="/openapi.json" if auth_config.docs_enabled else None,
)
install_auth(app, auth_config)
app.include_router(create_session_quality_router())
app.include_router(create_economics_router())
app.include_router(create_collector_router())
app.include_router(create_alert_delivery_router())
app.include_router(
    create_readiness_router(
        manifest_path=os.getenv(
            "MARKETPILOT_READINESS_MANIFEST",
            "data/readiness/readiness-manifest.json",
        ),
        shadow_ledger_path=os.getenv(
            "MARKETPILOT_SHADOW_LEDGER",
            "data/readiness/shadow-sessions.jsonl",
        ),
    )
)

CODE_VERSION = os.getenv("MARKETPILOT_CODE_VERSION", "development-unpinned")
RULES_PATH = os.getenv("MARKETPILOT_RULES_PATH", "config/rules-v1.toml")
PROMOTION_CRITERIA_PATH = os.getenv(
    "MARKETPILOT_PROMOTION_CRITERIA_PATH",
    "config/promotion-criteria-v1.toml",
)
rules_config = load_rules(RULES_PATH)
promotion_criteria = load_promotion_criteria(PROMOTION_CRITERIA_PATH)
runtime_persistence = create_runtime_persistence(os.environ)
audit_repository = runtime_persistence.audit
app.include_router(
    create_audit_router(
        audit_repository,
        backend=runtime_persistence.backend,
    )
)
app.include_router(create_validation_gate_router(promotion_criteria))
registry = ModelRegistry()
strikepilot_model = StrikePilotModel(
    strike_increment=rules_config.strike_increment,
    wing_width=rules_config.wing_width,
)
registry.register(strikepilot_model)
governance_service = GovernanceService(
    runtime_persistence.governance,
    baseline_artifact_hash=strikepilot_model.descriptor.artifact_hash,
)
app.include_router(create_governance_router(governance_service))
runner = DecisionRunner(registry, rules_version=rules_config.version)
decisions = DecisionStore(audit_repository)
capability_reports = CapabilityReportStore()
operations = OperationsService(audit_repository, code_version=CODE_VERSION)
stream_attribution_store = runtime_persistence.stream_attribution
stream_attribution = StreamAttributionService(stream_attribution_store, operations.alerts)
app.include_router(create_stream_attribution_router(stream_attribution))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "marketpilot-api"}


@app.get("/v1/models")
def models() -> list[dict[str, object]]:
    return [asdict(descriptor) for descriptor in registry.descriptors()]


@app.get("/v1/market/state")
def market_state() -> dict[str, object]:
    report = capability_reports.latest("webull")
    if report is not None:
        effective_quality = _effective_capability_quality(report.quality, report.production_ready)
        unavailable = [item.capability_id for item in report.results if item.status != "PASS"]
        return {
            "data_asof": report.probed_at.isoformat(),
            "quality": effective_quality.value,
            "stale_fields": unavailable,
            "reason": (
                "WEBULL_CAPABILITY_VERIFIED"
                if report.production_ready
                else "WEBULL_SCHEMA_OBSERVED_NOT_VERIFIED"
            ),
            "verification_status": report.verification_status,
            "production_ready": report.production_ready,
            "execution_enabled": False,
        }
    return {
        "data_asof": datetime.now(UTC).isoformat(),
        "quality": "RED",
        "stale_fields": ["ES", "SPX", "VIX", "SPXW_OPTION_CHAIN"],
        "reason": "DATA_CAPABILITY_NOT_VERIFIED",
        "execution_enabled": False,
    }


@app.get("/v1/providers/webull/capabilities")
def webull_capabilities() -> dict[str, object]:
    report = capability_reports.latest("webull")
    if report is None:
        return {
            "provider": "webull",
            "status": "NOT_RUN",
            "quality": "RED",
            "results": [],
        }
    return report.model_dump(mode="json")


@app.get("/v1/events/today")
def events_today() -> dict[str, object]:
    return {
        "status": "NOT_CONFIGURED",
        "events": [],
        "event_cleared": False,
        "message": "No authorized event source has been configured.",
    }


@app.get("/v1/overview", response_model=OverviewOutput)
def overview() -> OverviewOutput:
    state = market_state()
    return operations.overview(
        as_of=datetime.now(UTC),
        market_quality=str(state["quality"]),
        market_reason=str(state["reason"]),
    )


@app.get("/v1/demo/scenarios", response_model=list[DemoScenarioOutput])
def scenarios() -> list[DemoScenarioOutput]:
    return list(operations.scenarios())


@app.post("/v1/events/assess", response_model=EventAssessmentOutput)
def assess_event(request: EventAssessmentInput) -> EventAssessmentOutput:
    try:
        assessment = operations.assess_event(request.event, request.as_of)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return EventAssessmentOutput(assessment=assessment)


@app.get("/v1/alerts", response_model=AlertListOutput)
def alerts() -> AlertListOutput:
    return AlertListOutput(alerts=operations.alerts())


@app.get("/v1/alerts/{alert_id}/feedback", response_model=AlertFeedbackListOutput)
def alert_feedback_history(alert_id: str) -> AlertFeedbackListOutput:
    try:
        feedback = operations.feedback(alert_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="alert not found",
        ) from exc
    return AlertFeedbackListOutput(feedback=feedback)


@app.post("/v1/alerts/{alert_id}/feedback", response_model=AlertFeedbackOutput)
def alert_feedback(alert_id: str, request: AlertFeedbackInput) -> AlertFeedbackOutput:
    try:
        feedback, alert = operations.record_feedback(
            alert_id=alert_id,
            kind=request.kind,
            actor=request.actor,
            recorded_at=request.recorded_at,
            note=request.note,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="alert not found",
        ) from exc
    return AlertFeedbackOutput(feedback=feedback, alert=alert)


@app.post("/v1/decision/run", response_model=DecisionRunOutput)
def run_decision(request: DecisionRunInput) -> DecisionRunOutput:
    if request.run_mode is DecisionRunMode.SCENARIO:
        if request.gates is None:  # Defensive guard for callers bypassing Pydantic validation.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="gates are required for SCENARIO runs",
            )
        gates = DecisionGateContext(**request.gates.model_dump())
    else:
        gates = _live_gate_context()

    effective_as_of = _decision_as_of(request)
    governance_session_id = _governance_session_id(request, effective_as_of)
    governed_model_identity = _frozen_governed_model_identity(
        request.model_id,
        request.run_mode,
        governance_session_id,
    )
    frozen = freeze_snapshot(
        {
            "model_id": request.model_id,
            "run_mode": request.run_mode,
            "as_of": effective_as_of,
            "values": request.values,
            "gates": asdict(gates),
            "code_version": CODE_VERSION,
            "rules_version": rules_config.version,
            "governance_session_id": governance_session_id,
            "governed_model_identity": (
                asdict(governed_model_identity)
                if governed_model_identity is not None
                else None
            ),
        }
    )
    snapshot = MarketSnapshot(
        snapshot_id=frozen.snapshot_id,
        as_of=effective_as_of,
        quality=DataQualityReport(status=gates.data_quality),
        values=request.values,
    )
    try:
        result = runner.run(
            model_id=request.model_id,
            snapshot=snapshot,
            gates=gates,
            required_model_identity=governed_model_identity,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    response = DecisionRunOutput(
        run_id=result.run_id,
        run_mode=request.run_mode,
        model_id=result.model_id,
        model_version=result.model_version,
        model_artifact_hash=registry.get(result.model_id).descriptor.artifact_hash,
        governed_model_version=(
            governed_model_identity.version
            if governed_model_identity is not None
            else None
        ),
        governed_model_artifact_hash=(
            governed_model_identity.artifact_hash
            if governed_model_identity is not None
            else None
        ),
        governance_session_id=governance_session_id,
        rules_version=result.rules_version,
        code_version=CODE_VERSION,
        snapshot_id=result.snapshot_id,
        data_as_of=result.data_as_of,
        action=result.action,
        reasons=list(result.reasons),
        output=dict(result.output),
    )
    decisions.put(response)
    return response


def _frozen_governed_model_identity(
    model_id: str,
    run_mode: DecisionRunMode,
    session_id: str,
) -> ModelArtifactIdentity | None:
    try:
        frozen = runtime_persistence.governance.freeze_session(model_id, session_id)
        return ModelArtifactIdentity(
            version=frozen.version,
            artifact_hash=frozen.artifact_hash,
        )
    except GovernanceError:
        # SCENARIO keeps the explicitly labelled baseline demo usable before a local
        # champion is selected. LIVE must never operate without a governed champion.
        if run_mode is DecisionRunMode.SCENARIO:
            return None
        return ModelArtifactIdentity(
            version="GOVERNANCE_CHAMPION_MISSING",
            artifact_hash="GOVERNANCE_CHAMPION_MISSING",
        )
    except Exception:
        # A governance backend outage must freeze decisions, not fall back to an
        # arbitrary loaded model. The sentinel can never equal a real version.
        # The broad catch is deliberate fail-closed behavior, but it must be
        # audible: log the underlying failure instead of masking it silently.
        logger.exception(
            "governance backend failure during session freeze for model %s; "
            "decision fails closed",
            model_id,
        )
        return ModelArtifactIdentity(
            version="GOVERNANCE_BACKEND_UNAVAILABLE",
            artifact_hash="GOVERNANCE_BACKEND_UNAVAILABLE",
        )


def _decision_as_of(request: DecisionRunInput) -> datetime:
    if request.run_mode is DecisionRunMode.LIVE:
        return _server_now()
    if request.as_of is None:  # Defensive guard for callers bypassing validation.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="as_of is required for SCENARIO runs",
        )
    return request.as_of


def _governance_session_id(request: DecisionRunInput, as_of: datetime) -> str:
    if request.run_mode is DecisionRunMode.LIVE:
        # The XNYS session label is derived solely from the server clock in the
        # configured exchange timezone. No client timestamp or session identifier is used.
        session_date = as_of.astimezone(ZoneInfo(rules_config.timezone)).date()
        return f"LIVE:XNYS:{session_date.isoformat()}"
    if request.scenario_session_id is None:  # Defensive guard for bypassed validation.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="scenario_session_id is required for SCENARIO runs",
        )
    return f"SCENARIO:{request.scenario_session_id}"


def _server_now() -> datetime:
    return datetime.now(UTC)


def _live_gate_context() -> DecisionGateContext:
    """Derive live safety gates only from server-owned state.

    Event and edge services are not configured in Phase 0, so those gates remain closed
    even when a provider capability report is Green.
    """

    report = capability_reports.latest("webull")
    if report is None:
        return DecisionGateContext(
            data_quality=DataQuality.RED,
            contract_matches=False,
            event_cleared=False,
            option_chain_usable=False,
            edge_ok=False,
        )

    capabilities = {item.capability_id: item.status for item in report.results}
    return DecisionGateContext(
        data_quality=_effective_capability_quality(report.quality, report.production_ready),
        contract_matches=(capabilities.get("es_explicit_contract") is CapabilityStatus.PASS),
        event_cleared=False,
        option_chain_usable=all(
            capabilities.get(capability_id) is CapabilityStatus.PASS
            for capability_id in ("spxw_contract_discovery", "spxw_option_snapshot")
        ),
        edge_ok=False,
    )


def _effective_capability_quality(
    reported_quality: DataQuality,
    production_ready: bool,
) -> DataQuality:
    if production_ready:
        return reported_quality
    return DataQuality.RED if reported_quality is DataQuality.RED else DataQuality.AMBER


@app.get("/v1/decisions/{run_id}", response_model=DecisionRunOutput)
def get_decision(run_id: str) -> DecisionRunOutput:
    decision = decisions.get(run_id)
    if decision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="decision not found")
    return decision


@app.get("/v1/model/health")
def model_health() -> dict[str, object]:
    return {
        "status": "NOT_CALIBRATED",
        "message": "Baseline skeleton only; walk-forward calibration is not yet available.",
        "models": [descriptor.model_id for descriptor in registry.descriptors()],
    }
