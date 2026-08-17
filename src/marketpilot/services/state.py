from __future__ import annotations

from marketpilot.services.persistence_contracts import AuditRepository
from marketpilot.services.repository import SQLiteAuditRepository
from marketpilot.services.schemas import DecisionRunOutput


class DecisionStore:
    """Decision audit facade backed by the injected append-only repository."""

    def __init__(self, repository: AuditRepository | None = None) -> None:
        self._repository = repository or SQLiteAuditRepository(":memory:")

    def put(self, decision: DecisionRunOutput) -> None:
        self._repository.append_decision(decision)

    def get(self, run_id: str) -> DecisionRunOutput | None:
        return self._repository.get_decision(run_id)
