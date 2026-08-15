import pytest

from marketpilot.models.registry import ModelRegistry
from marketpilot.models.strikepilot.model import StrikePilotModel


def test_registry_resolves_model_by_id() -> None:
    registry = ModelRegistry()
    model = StrikePilotModel()
    registry.register(model)
    assert registry.get("strikepilot_spxw_0dte_ic") is model


def test_registry_rejects_duplicate_model_id() -> None:
    registry = ModelRegistry()
    registry.register(StrikePilotModel())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(StrikePilotModel())
