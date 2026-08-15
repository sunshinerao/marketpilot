from __future__ import annotations

from marketpilot.models.base import DecisionModel, ModelDescriptor


class ModelRegistry:
    def __init__(self) -> None:
        self._models: dict[str, DecisionModel] = {}

    def register(self, model: DecisionModel) -> None:
        model_id = model.descriptor.model_id
        if model_id in self._models:
            raise ValueError(f"model already registered: {model_id}")
        self._models[model_id] = model

    def get(self, model_id: str) -> DecisionModel:
        try:
            return self._models[model_id]
        except KeyError as exc:
            raise KeyError(f"unknown model: {model_id}") from exc

    def descriptors(self) -> tuple[ModelDescriptor, ...]:
        return tuple(model.descriptor for model in self._models.values())
