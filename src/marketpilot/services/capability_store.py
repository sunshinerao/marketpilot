from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from marketpilot.domain.capabilities import CapabilityReport


class CapabilityReportStore:
    def __init__(self, root: str | Path = "data/capability-probes") -> None:
        self._root = Path(root)

    def save(self, report: CapabilityReport) -> Path:
        provider_dir = self._root / report.provider
        provider_dir.mkdir(parents=True, exist_ok=True)
        timestamp = report.probed_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = provider_dir / f"{timestamp}.json"
        serialized = report.model_dump_json(indent=2)
        self._atomic_write(destination, serialized)
        self._atomic_write(provider_dir / "latest.json", serialized)
        return destination

    def latest(self, provider: str) -> CapabilityReport | None:
        path = self._root / provider / "latest.json"
        if not path.exists():
            return None
        try:
            return CapabilityReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError):
            # A partial/corrupt report must never become capability evidence or take
            # down the read-only API. Callers treat absence as RED/NO_TRADE.
            return None

    @staticmethod
    def _atomic_write(destination: Path, content: str) -> None:
        temporary = destination.with_suffix(f".{datetime.now(UTC).timestamp():.6f}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(destination)
