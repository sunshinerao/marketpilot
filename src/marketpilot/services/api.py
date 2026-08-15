from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI

from marketpilot.models.registry import ModelRegistry
from marketpilot.models.strikepilot.model import StrikePilotModel

app = FastAPI(title="MarketPilot API", version="0.1.0")

registry = ModelRegistry()
registry.register(StrikePilotModel())


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "marketpilot-api"}


@app.get("/v1/models")
def models() -> list[dict[str, object]]:
    return [asdict(descriptor) for descriptor in registry.descriptors()]


@app.get("/v1/model/health")
def model_health() -> dict[str, object]:
    return {
        "status": "NOT_CALIBRATED",
        "message": "Baseline skeleton only; walk-forward calibration is not yet available.",
        "models": [descriptor.model_id for descriptor in registry.descriptors()],
    }
