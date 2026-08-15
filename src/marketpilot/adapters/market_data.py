from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    provider: str
    probed_at: datetime
    capabilities: Mapping[str, bool]
    missing_fields: Mapping[str, Sequence[str]]
    notes: tuple[str, ...] = ()


class MarketDataAdapter(Protocol):
    """Provider-neutral contract. Concrete field mappings require a capability probe."""

    @property
    def provider_name(self) -> str: ...

    async def probe_capabilities(self) -> CapabilityResult: ...
