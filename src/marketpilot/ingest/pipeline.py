from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from marketpilot.adapters.databento import DatabentoApiError, DayPull, HistoricalGateway
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


class IngestCostCeilingExceeded(RuntimeError):
    """Raised when a pull plan exceeds the approved cost ceiling."""


class DayStatus(StrEnum):
    LANDED = "LANDED"
    SKIPPED_PRESENT = "SKIPPED_PRESENT"
    NOT_DUE = "NOT_DUE"
    GAP = "GAP"


@dataclass(frozen=True, slots=True)
class PlannedDay:
    pull: DayPull
    estimated_usd: float


@dataclass(frozen=True, slots=True)
class PullPlan:
    plan_id: str
    created_at: datetime
    ceiling_usd: float
    total_estimated_usd: float
    items: tuple[PlannedDay, ...]


@dataclass(frozen=True, slots=True)
class DayOutcome:
    logical_key: str
    status: DayStatus
    detail: str


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
    ) -> None:
        self._gateway = gateway
        self._landing = landing
        self._pit_ledger = pit_ledger
        self._cost_ledger = cost_ledger

    def build_plan(
        self,
        pulls: list[DayPull],
        *,
        ceiling_usd: float,
    ) -> PullPlan:
        created_at = datetime.now(UTC)
        items = tuple(
            PlannedDay(pull=pull, estimated_usd=self._gateway.estimate_cost(pull))
            for pull in pulls
        )
        total = round(sum(item.estimated_usd for item in items), 6)
        plan_id = freeze_snapshot(
            {
                "created_at": created_at,
                "ceiling_usd": ceiling_usd,
                "items": [
                    {"logical_key": item.pull.logical_key, "estimated_usd": item.estimated_usd}
                    for item in items
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
            items=items,
        )

    def run(self, plan: PullPlan) -> PullReport:
        started_at = datetime.now(UTC)
        outcomes: list[DayOutcome] = []
        for item in plan.items:
            outcomes.append(self._run_day(item.pull, item.estimated_usd))
        return PullReport(
            plan_id=plan.plan_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            outcomes=tuple(outcomes),
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
