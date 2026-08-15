from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, status

from marketpilot.decision.gates import DecisionGateContext
from marketpilot.decision.runner import DecisionRunner
from marketpilot.domain.market import DataQualityReport, MarketSnapshot
from marketpilot.domain.snapshot import freeze_snapshot
from marketpilot.models.registry import ModelRegistry
from marketpilot.models.strikepilot.model import StrikePilotModel
from marketpilot.services.schemas import DecisionRunInput, DecisionRunOutput
from marketpilot.services.state import DecisionStore

app = FastAPI(title="MarketPilot API", version="0.1.0")

registry = ModelRegistry()
registry.register(StrikePilotModel())
runner = DecisionRunner(registry)
decisions = DecisionStore()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "marketpilot-api"}


@app.get("/v1/models")
def models() -> list[dict[str, object]]:
    return [asdict(descriptor) for descriptor in registry.descriptors()]


@app.get("/v1/market/state")
def market_state() -> dict[str, object]:
    return {
        "data_asof": datetime.now(UTC).isoformat(),
        "quality": "RED",
        "stale_fields": ["ES", "SPX", "VIX", "SPXW_OPTION_CHAIN"],
        "reason": "DATA_CAPABILITY_NOT_VERIFIED",
        "execution_enabled": False,
    }


@app.get("/v1/events/today")
def events_today() -> dict[str, object]:
    return {
        "status": "NOT_CONFIGURED",
        "events": [],
        "event_cleared": False,
        "message": "No authorized event source has been configured.",
    }


@app.post("/v1/decision/run", response_model=DecisionRunOutput)
def run_decision(request: DecisionRunInput) -> DecisionRunOutput:
    frozen = freeze_snapshot(
        {
            "model_id": request.model_id,
            "as_of": request.as_of,
            "values": request.values,
            "gates": request.gates.model_dump(mode="json"),
        }
    )
    snapshot = MarketSnapshot(
        snapshot_id=frozen.snapshot_id,
        as_of=request.as_of,
        quality=DataQualityReport(status=request.gates.data_quality),
        values=request.values,
    )
    try:
        result = runner.run(
            model_id=request.model_id,
            snapshot=snapshot,
            gates=DecisionGateContext(**request.gates.model_dump()),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    response = DecisionRunOutput(
        run_id=result.run_id,
        model_id=result.model_id,
        model_version=result.model_version,
        rules_version=result.rules_version,
        snapshot_id=result.snapshot_id,
        data_as_of=result.data_as_of,
        action=result.action,
        reasons=list(result.reasons),
        output=dict(result.output),
    )
    decisions.put(response)
    return response


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
