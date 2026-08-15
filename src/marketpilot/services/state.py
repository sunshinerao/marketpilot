from __future__ import annotations

from threading import RLock

from marketpilot.services.schemas import DecisionRunOutput


class DecisionStore:
    """Process-local store used until the Phase 2 PostgreSQL repository is implemented."""

    def __init__(self) -> None:
        self._items: dict[str, DecisionRunOutput] = {}
        self._lock = RLock()

    def put(self, decision: DecisionRunOutput) -> None:
        with self._lock:
            self._items[decision.run_id] = decision

    def get(self, run_id: str) -> DecisionRunOutput | None:
        with self._lock:
            return self._items.get(run_id)
