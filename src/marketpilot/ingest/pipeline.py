from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from marketpilot.adapters.databento import (
    DatabentoApiError,
    DayPull,
    HistoricalGateway,
    enumerate_expiring,
    spxw_definitions_pull,
)
from marketpilot.domain.point_in_time import PointInTimeRecord
from marketpilot.domain.snapshot import freeze_snapshot
from marketpilot.ingest.cost_ledger import CostLedger
from marketpilot.ingest.pit_ledger import PitBatchLedger
from marketpilot.services.raw_landing import LicensedPayloadLandingService, SensitivePayload

NEW_YORK = ZoneInfo("America/New_York")
PROVIDER = "databento"
PROVIDER_VERSION = "databento-historical-v1"
LANDING_PRINCIPAL = "ingest-pipeline"
LANDING_PURPOSE = "licensed-history-pull"
SPXW_0DTE_SCOPE = "spxw-0dte"
DEFINITIONS_CONTENT_TYPE = "text/csv"


class IngestCostCeilingExceeded(RuntimeError):
    """Raised when a pull plan exceeds the approved cost ceiling."""


class DayStatus(StrEnum):
    LANDED = "LANDED"
    SKIPPED_PRESENT = "SKIPPED_PRESENT"
    NOT_DUE = "NOT_DUE"
    GAP = "GAP"
    EMPTY_CHAIN = "EMPTY_CHAIN"


class ChainResolver(Protocol):
    """Resolves the contracts expiring on a trading day; caches per day."""

    def resolve(self, day: date) -> tuple[str, ...]: ...

    def definitions_payload(self, day: date) -> bytes:
        """The cached definition CSV the enumeration was derived from."""
        ...


class DatabentoChainResolver:
    """Downloads each day's definition CSV once and caches the enumeration."""

    def __init__(self, gateway: HistoricalGateway) -> None:
        self._gateway = gateway
        self._cache: dict[date, tuple[bytes, tuple[str, ...]]] = {}

    def resolve(self, day: date) -> tuple[str, ...]:
        return self._cached(day)[1]

    def definitions_payload(self, day: date) -> bytes:
        return self._cached(day)[0]

    def _cached(self, day: date) -> tuple[bytes, tuple[str, ...]]:
        if day not in self._cache:
            payload = self._gateway.download_definitions(day)
            self._cache[day] = (payload, enumerate_expiring(payload, day))
        return self._cache[day]


@dataclass(frozen=True, slots=True)
class PlannedDay:
    pull: DayPull
    estimated_usd: float


@dataclass(frozen=True, slots=True)
class DayOutcome:
    logical_key: str
    status: DayStatus
    detail: str


@dataclass(frozen=True, slots=True)
class PullPlan:
    plan_id: str
    created_at: datetime
    ceiling_usd: float
    total_estimated_usd: float
    items: tuple[PlannedDay, ...]
    empty_chain_outcomes: tuple[DayOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class PullReport:
    plan_id: str
    started_at: datetime
    finished_at: datetime
    outcomes: tuple[DayOutcome, ...]

    def count(self, status: DayStatus) -> int:
        return sum(outcome.status is status for outcome in self.outcomes)


class IngestPipeline:
    """Cost-gated licensed history pulls into the encrypted landing boundary."""

    def __init__(
        self,
        *,
        gateway: HistoricalGateway,
        landing: LicensedPayloadLandingService,
        pit_ledger: PitBatchLedger,
        cost_ledger: CostLedger,
        report_path: Path | None = None,
    ) -> None:
        self._gateway = gateway
        self._landing = landing
        self._pit_ledger = pit_ledger
        self._cost_ledger = cost_ledger
        self._report_path = report_path

    def build_plan(
        self,
        pulls: list[DayPull],
        *,
        ceiling_usd: float,
        chain_resolver: ChainResolver | None = None,
    ) -> PullPlan:
        created_at = datetime.now(UTC)
        items: list[PlannedDay] = []
        empty_chain_outcomes: list[DayOutcome] = []
        enumeration_usd = 0.0
        for pull in pulls:
            if pull.scope == SPXW_0DTE_SCOPE:
                if chain_resolver is None:
                    raise ValueError(f"scope {SPXW_0DTE_SCOPE!r} requires a chain resolver")
                if self._pit_ledger.find(pull.logical_key) is not None:
                    # Already landed: run() reports SKIPPED_PRESENT; do not spend
                    # on enumeration just to estimate an already-present day.
                    items.append(PlannedDay(pull=pull, estimated_usd=0.0))
                    continue
                resolved, estimate = self._resolve_zero_dte(pull, chain_resolver)
                enumeration_usd += estimate
                if resolved is None:
                    empty_chain_outcomes.append(
                        DayOutcome(
                            pull.logical_key,
                            DayStatus.EMPTY_CHAIN,
                            "no contracts expiring this day",
                        )
                    )
                    continue
                pull = resolved
            items.append(
                PlannedDay(pull=pull, estimated_usd=self._gateway.estimate_cost(pull))
            )
        total = round(sum(item.estimated_usd for item in items) + enumeration_usd, 6)
        plan_id = freeze_snapshot(
            {
                "created_at": created_at,
                "ceiling_usd": ceiling_usd,
                "items": [
                    {"logical_key": item.pull.logical_key, "estimated_usd": item.estimated_usd}
                    for item in items
                ],
                "enumeration_usd": round(enumeration_usd, 6),
                "empty_chain_days": [
                    outcome.logical_key for outcome in empty_chain_outcomes
                ],
            }
        ).snapshot_id
        within_ceiling = total <= ceiling_usd
        self._cost_ledger.append(
            plan_id=plan_id,
            estimated_usd=total,
            ceiling_usd=ceiling_usd,
            decision="APPROVED" if within_ceiling else "BLOCKED",
        )
        if not within_ceiling:
            raise IngestCostCeilingExceeded(
                f"estimated ${total:.2f} exceeds the ${ceiling_usd:.2f} ceiling"
            )
        return PullPlan(
            plan_id=plan_id,
            created_at=created_at,
            ceiling_usd=ceiling_usd,
            total_estimated_usd=total,
            items=tuple(items),
            empty_chain_outcomes=tuple(empty_chain_outcomes),
        )

    def _resolve_zero_dte(
        self,
        pull: DayPull,
        resolver: ChainResolver,
    ) -> tuple[DayPull | None, float]:
        """Enumerate the day's 0DTE chain and land the definition CSV for audit.

        Returns the pull with the enumerated raw symbols filled in, or None when
        no contract expires that day. The float is the definition-download
        estimate: real spend even when the chain turns out empty.
        """

        definitions = spxw_definitions_pull(pull.day)
        estimate = self._gateway.estimate_cost(definitions)
        symbols = resolver.resolve(pull.day)
        self._land_definitions(definitions, resolver.definitions_payload(pull.day), estimate)
        if not symbols:
            return None, estimate
        return replace(pull, symbols=symbols), estimate

    def _land_definitions(self, pull: DayPull, payload: bytes, estimated_usd: float) -> None:
        """Land the definition CSV used for enumeration as its own PIT batch."""

        key = pull.logical_key
        if self._pit_ledger.find(key) is not None:
            return
        landed_at = datetime.now(UTC)
        published_at = datetime.combine(pull.day, time(23, 59, 59), tzinfo=NEW_YORK).astimezone(
            UTC
        )
        if published_at > landed_at:
            return  # the PIT invariant forbids landing before the day closes
        receipt = self._landing.land(
            provider=PROVIDER,
            dataset=pull.dataset,
            logical_key=key,
            published_at=published_at,
            first_seen_at=landed_at,
            payload=SensitivePayload(payload),
            content_type=DEFINITIONS_CONTENT_TYPE,
            principal=LANDING_PRINCIPAL,
            purpose=LANDING_PURPOSE,
        )
        record = PointInTimeRecord.create(
            logical_key=key,
            published_at=published_at,
            first_seen_at=landed_at,
            provider=PROVIDER,
            provider_version=PROVIDER_VERSION,
            schema_version=pull.schema,
            content={
                "dataset": pull.dataset,
                "schema": pull.schema,
                "day": pull.day.isoformat(),
                "scope": pull.scope,
                "object_key": receipt.object_key,
                "plaintext_sha256": receipt.plaintext_sha256,
                "record_count": max(len(payload.decode("utf-8").splitlines()) - 1, 0),
                "estimated_usd": estimated_usd,
            },
        )
        self._pit_ledger.append(record)

    def run(self, plan: PullPlan) -> PullReport:
        started_at = datetime.now(UTC)
        outcomes: list[DayOutcome] = []
        for item in plan.items:
            outcomes.append(self._run_day(item.pull, item.estimated_usd))
        outcomes.extend(plan.empty_chain_outcomes)
        report = PullReport(
            plan_id=plan.plan_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            outcomes=tuple(outcomes),
        )
        self._persist_report(report)
        return report

    def _persist_report(self, report: PullReport) -> None:
        """Append the run outcome, including per-day gap reasons, to JSONL."""

        if self._report_path is None:
            return
        self._report_path.parent.mkdir(parents=True, exist_ok=True)
        with self._report_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "plan_id": report.plan_id,
                        "started_at": report.started_at.isoformat(),
                        "finished_at": report.finished_at.isoformat(),
                        "outcomes": [
                            {
                                "logical_key": outcome.logical_key,
                                "status": outcome.status.value,
                                "detail": outcome.detail,
                            }
                            for outcome in report.outcomes
                        ],
                    },
                    sort_keys=True,
                )
                + "\n"
            )

    def _run_day(self, pull: DayPull, estimated_usd: float) -> DayOutcome:
        key = pull.logical_key
        if self._pit_ledger.find(key) is not None:
            return DayOutcome(key, DayStatus.SKIPPED_PRESENT, "already landed")

        pulled_at = datetime.now(UTC)
        published_at = datetime.combine(pull.day, time(23, 59, 59), tzinfo=NEW_YORK).astimezone(
            UTC
        )
        if published_at > pulled_at:
            return DayOutcome(key, DayStatus.NOT_DUE, "day has not closed yet")

        try:
            payload = self._gateway.download_day(pull)
        except DatabentoApiError as exc:
            return DayOutcome(key, DayStatus.GAP, f"download failed ({exc.case})")

        receipt = self._landing.land(
            provider=PROVIDER,
            dataset=pull.dataset,
            logical_key=key,
            published_at=published_at,
            first_seen_at=pulled_at,
            payload=SensitivePayload(payload),
            content_type="application/x-dbn",
            principal=LANDING_PRINCIPAL,
            purpose=LANDING_PURPOSE,
        )
        try:
            record_count: int | None = self._gateway.record_count(pull)
        except DatabentoApiError:
            record_count = None
        record = PointInTimeRecord.create(
            logical_key=key,
            published_at=published_at,
            first_seen_at=pulled_at,
            provider=PROVIDER,
            provider_version=PROVIDER_VERSION,
            schema_version=pull.schema,
            content={
                "dataset": pull.dataset,
                "schema": pull.schema,
                "day": pull.day.isoformat(),
                "scope": pull.scope,
                "object_key": receipt.object_key,
                "plaintext_sha256": receipt.plaintext_sha256,
                "record_count": record_count,
                "estimated_usd": estimated_usd,
            },
        )
        self._pit_ledger.append(record)
        return DayOutcome(key, DayStatus.LANDED, record.record_id)
