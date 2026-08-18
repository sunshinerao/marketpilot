from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from marketpilot.domain.snapshot import freeze_snapshot

GENESIS_HASH = "sha256:cost-ledger-genesis"


class CostLedgerError(ValueError):
    """Raised when the append-only cost ledger fails integrity verification."""


@dataclass(frozen=True, slots=True)
class CostLedgerEntry:
    sequence: int
    recorded_at: datetime
    plan_id: str
    estimated_usd: float
    ceiling_usd: float
    decision: str
    previous_hash: str
    entry_hash: str


class CostLedger:
    """Hash-chained append-only record of every cost-gate decision."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def append(
        self,
        *,
        plan_id: str,
        estimated_usd: float,
        ceiling_usd: float,
        decision: str,
    ) -> CostLedgerEntry:
        entries = self.load()
        previous_hash = entries[-1].entry_hash if entries else GENESIS_HASH
        recorded_at = datetime.now(UTC)
        entry_hash = freeze_snapshot(
            {
                "sequence": len(entries),
                "recorded_at": recorded_at,
                "plan_id": plan_id,
                "estimated_usd": estimated_usd,
                "ceiling_usd": ceiling_usd,
                "decision": decision,
                "previous_hash": previous_hash,
            }
        ).snapshot_id
        entry = CostLedgerEntry(
            sequence=len(entries),
            recorded_at=recorded_at,
            plan_id=plan_id,
            estimated_usd=estimated_usd,
            ceiling_usd=ceiling_usd,
            decision=decision,
            previous_hash=previous_hash,
            entry_hash=entry_hash,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "sequence": entry.sequence,
                        "recorded_at": entry.recorded_at.isoformat(),
                        "plan_id": entry.plan_id,
                        "estimated_usd": entry.estimated_usd,
                        "ceiling_usd": entry.ceiling_usd,
                        "decision": entry.decision,
                        "previous_hash": entry.previous_hash,
                        "entry_hash": entry.entry_hash,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        return entry

    def load(self) -> tuple[CostLedgerEntry, ...]:
        if not self._path.exists():
            return ()
        entries: list[CostLedgerEntry] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            entries.append(
                CostLedgerEntry(
                    sequence=int(raw["sequence"]),
                    recorded_at=datetime.fromisoformat(raw["recorded_at"]),
                    plan_id=str(raw["plan_id"]),
                    estimated_usd=float(raw["estimated_usd"]),
                    ceiling_usd=float(raw["ceiling_usd"]),
                    decision=str(raw["decision"]),
                    previous_hash=str(raw["previous_hash"]),
                    entry_hash=str(raw["entry_hash"]),
                )
            )
        self._verify(entries)
        return tuple(entries)

    @staticmethod
    def _verify(entries: list[CostLedgerEntry]) -> None:
        previous_hash = GENESIS_HASH
        for index, entry in enumerate(entries):
            if entry.sequence != index or entry.previous_hash != previous_hash:
                raise CostLedgerError("cost ledger chain is broken")
            expected = freeze_snapshot(
                {
                    "sequence": entry.sequence,
                    "recorded_at": entry.recorded_at,
                    "plan_id": entry.plan_id,
                    "estimated_usd": entry.estimated_usd,
                    "ceiling_usd": entry.ceiling_usd,
                    "decision": entry.decision,
                    "previous_hash": entry.previous_hash,
                }
            ).snapshot_id
            if expected != entry.entry_hash:
                raise CostLedgerError("cost ledger entry hash mismatch")
            previous_hash = entry.entry_hash
