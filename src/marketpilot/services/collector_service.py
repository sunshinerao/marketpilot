from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from marketpilot.domain.capabilities import CapabilityReport
from marketpilot.domain.collector import (
    CollectorEvent,
    CollectorEventKind,
    CollectorPolicy,
    CollectorRunResult,
    CollectorState,
    CollectorTrace,
)
from marketpilot.domain.data_quality import QualityPolicy, QuoteObservation, QuoteQualityEvaluator
from marketpilot.domain.market import DataQuality
from marketpilot.domain.point_in_time import PointInTimeRecord
from marketpilot.domain.snapshot import freeze_snapshot


class CollectorOrchestrator:
    """Deterministic provider-neutral collector simulator.

    It never performs network I/O or execution. Every input is frozen as a point-in-time
    record, making failure scenarios reproducible without provider credentials.
    """

    def __init__(
        self,
        *,
        provider: str,
        provider_version: str,
        expected_schema_version: str,
        expected_instrument_id: str,
        capability: CapabilityReport,
        policy: CollectorPolicy,
        quality_policy: QualityPolicy,
        session_gate: Callable[[datetime], tuple[str, ...]] | None = None,
    ) -> None:
        for name, value in (
            ("provider", provider),
            ("provider_version", provider_version),
            ("expected_schema_version", expected_schema_version),
            ("expected_instrument_id", expected_instrument_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        if capability.provider != provider:
            raise ValueError("capability provider must match collector provider")
        self._provider = provider
        self._provider_version = provider_version
        self._schema_version = expected_schema_version
        self._instrument_id = expected_instrument_id
        self._capability = capability
        self._policy = policy
        self._quality_evaluator = QuoteQualityEvaluator(quality_policy)
        self._session_gate = session_gate

    def run(self, events: tuple[CollectorEvent, ...]) -> CollectorRunResult:
        state = CollectorState.STOPPED
        next_retry_at: datetime | None = None
        attempts = 0
        watermark: datetime | None = None
        last_observation_at: datetime | None = None
        seen_events: set[str] = set()
        seen_observations: set[str] = set()
        latest_by_source: dict[str, QuoteObservation] = {}
        records: list[PointInTimeRecord] = []
        traces: list[CollectorTrace] = []
        run_reasons = set(self._capability_reasons())
        accepted_quotes = 0
        duplicate_events = 0
        duplicate_observations = 0
        out_of_order = 0
        quality_report = None
        previous_first_seen_at: datetime | None = None

        for event in events:
            record = event.point_in_time_record(
                provider=self._provider,
                provider_version=self._provider_version,
            )
            records.append(record)
            reasons: set[str] = set()
            accepted = True

            if event.event_id in seen_events:
                accepted = False
                duplicate_events += 1
                reasons.add("DUPLICATE_EVENT")
            else:
                seen_events.add(event.event_id)

            if previous_first_seen_at is not None and event.first_seen_at < previous_first_seen_at:
                accepted = False
                reasons.add("EVENT_TIME_REGRESSION")
            previous_first_seen_at = max(previous_first_seen_at, event.first_seen_at) if (
                previous_first_seen_at is not None
            ) else event.first_seen_at

            if accepted and event.schema_version != self._schema_version:
                accepted = False
                state = CollectorState.HALTED
                reasons.add("SCHEMA_DRIFT")
                run_reasons.add("SCHEMA_DRIFT")
            elif accepted and state is CollectorState.HALTED:
                accepted = False
                reasons.add("COLLECTOR_HALTED")
            elif accepted:
                (
                    state,
                    next_retry_at,
                    attempts,
                    watermark,
                    last_observation_at,
                    accepted_quotes,
                    duplicate_observations,
                    out_of_order,
                ) = self._apply_event(
                    event=event,
                    state=state,
                    next_retry_at=next_retry_at,
                    attempts=attempts,
                    watermark=watermark,
                    last_observation_at=last_observation_at,
                    seen_observations=seen_observations,
                    latest_by_source=latest_by_source,
                    reasons=reasons,
                    accepted_quotes=accepted_quotes,
                    duplicate_observations=duplicate_observations,
                    out_of_order=out_of_order,
                )
                accepted = not reasons

            quality_report = (
                self._quality_evaluator.evaluate(
                    tuple(latest_by_source.values()), as_of=event.first_seen_at
                )
                if latest_by_source
                else None
            )
            state, freshness_reason = self._apply_freshness(
                state, event.first_seen_at, last_observation_at
            )
            if freshness_reason is not None:
                reasons.add(freshness_reason)
            run_reasons.update(
                reasons.intersection(
                    {"SCHEMA_DRIFT", "RECONNECT_BUDGET_EXHAUSTED", "INVALID_RETRY_AFTER"}
                )
            )

            trace_quality = quality_report.status if quality_report else DataQuality.RED
            trace_freeze = quality_report.freeze if quality_report else True
            output_record = PointInTimeRecord.create(
                logical_key=f"collector-output:{self._provider}:{event.event_id}",
                published_at=event.first_seen_at,
                first_seen_at=event.first_seen_at,
                provider="marketpilot",
                provider_version="collector-v1",
                schema_version="collector-output-v1",
                content={
                    "event_id": event.event_id,
                    "state": state.value,
                    "accepted": accepted,
                    "reasons": sorted(reasons),
                    "watermark": watermark,
                    "next_retry_at": next_retry_at,
                    "reconnect_attempts": attempts,
                    "quality": trace_quality.value,
                    "freeze": trace_freeze,
                },
            )
            records.append(output_record)
            traces.append(
                CollectorTrace(
                    event_id=event.event_id,
                    state=state,
                    accepted=accepted,
                    reasons=tuple(sorted(reasons)),
                    input_record_id=record.record_id,
                    input_content_hash=record.content_hash,
                    output_record_id=output_record.record_id,
                    output_content_hash=output_record.content_hash,
                    watermark=watermark,
                    next_retry_at=next_retry_at,
                    reconnect_attempts=attempts,
                    quality=trace_quality,
                    freeze=trace_freeze,
                )
            )

        if state is not CollectorState.STREAMING:
            run_reasons.add(f"COLLECTOR_{state.value}")
        if events and self._session_gate is not None:
            run_reasons.update(self._session_gate(events[-1].first_seen_at))
        if quality_report is None:
            run_reasons.add("NO_QUALITY_REPORT")
        elif not quality_report.permits_decision:
            run_reasons.update(quality_report.reasons or ("QUALITY_NOT_GREEN",))
        return CollectorRunResult(
            state=state,
            traces=tuple(traces),
            records=tuple(records),
            quality_report=quality_report,
            reasons=tuple(sorted(run_reasons)),
            accepted_quotes=accepted_quotes,
            duplicate_events=duplicate_events,
            duplicate_observations=duplicate_observations,
            out_of_order_observations=out_of_order,
            watermark=watermark,
            next_retry_at=next_retry_at,
        )

    def _capability_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self._capability.configured:
            reasons.append("CAPABILITY_NOT_CONFIGURED")
        if self._capability.verification_status != "VERIFIED":
            reasons.append("CAPABILITY_UNVERIFIED")
        if not self._capability.production_ready:
            reasons.append("CAPABILITY_NOT_PRODUCTION_READY")
        if self._capability.quality is not DataQuality.GREEN:
            reasons.append("CAPABILITY_QUALITY_NOT_GREEN")
        return tuple(reasons)

    def _apply_event(
        self,
        *,
        event: CollectorEvent,
        state: CollectorState,
        next_retry_at: datetime | None,
        attempts: int,
        watermark: datetime | None,
        last_observation_at: datetime | None,
        seen_observations: set[str],
        latest_by_source: dict[str, QuoteObservation],
        reasons: set[str],
        accepted_quotes: int,
        duplicate_observations: int,
        out_of_order: int,
    ) -> tuple[
        CollectorState,
        datetime | None,
        int,
        datetime | None,
        datetime | None,
        int,
        int,
        int,
    ]:
        kind = event.kind
        if kind is CollectorEventKind.START:
            if state is not CollectorState.STOPPED:
                reasons.add("INVALID_TRANSITION")
            else:
                state = CollectorState.CONNECTING
        elif kind is CollectorEventKind.CONNECTED:
            if state not in {
                CollectorState.CONNECTING,
                CollectorState.BACKING_OFF,
                CollectorState.RATE_LIMITED,
                CollectorState.DEGRADED,
            }:
                reasons.add("INVALID_TRANSITION")
            elif next_retry_at is not None and event.first_seen_at < next_retry_at:
                reasons.add("RETRY_TOO_EARLY")
            else:
                state = CollectorState.STREAMING
                next_retry_at = None
                attempts = 0
        elif kind is CollectorEventKind.CONNECTION_LOST:
            latest_by_source.clear()
            last_observation_at = None
            attempts += 1
            if attempts > self._policy.max_reconnect_attempts:
                state = CollectorState.HALTED
                next_retry_at = None
                reasons.add("RECONNECT_BUDGET_EXHAUSTED")
            else:
                state = CollectorState.BACKING_OFF
                next_retry_at = event.first_seen_at + self._policy.reconnect_delay(attempts)
        elif kind is CollectorEventKind.RATE_LIMITED:
            latest_by_source.clear()
            last_observation_at = None
            retry_seconds = event.payload.get("retry_after_seconds")
            if not isinstance(retry_seconds, (int, float)) or retry_seconds <= 0:
                state = CollectorState.HALTED
                next_retry_at = None
                reasons.add("INVALID_RETRY_AFTER")
            else:
                state = CollectorState.RATE_LIMITED
                next_retry_at = event.first_seen_at + max(
                    timedelta(seconds=retry_seconds), self._policy.base_backoff
                )
        elif kind is CollectorEventKind.SCHEMA_DRIFT:
            state = CollectorState.HALTED
            reasons.add("SCHEMA_DRIFT")
        elif kind is CollectorEventKind.STOP:
            state = CollectorState.STOPPED
            next_retry_at = None
            latest_by_source.clear()
            last_observation_at = None
        elif kind is CollectorEventKind.HEARTBEAT:
            if state not in {CollectorState.STREAMING, CollectorState.DEGRADED}:
                reasons.add("INVALID_TRANSITION")
        elif kind is CollectorEventKind.QUOTE:
            if state not in {CollectorState.STREAMING, CollectorState.DEGRADED}:
                reasons.add("COLLECTOR_NOT_STREAMING")
            else:
                observation = event.observation
                if observation is None:  # domain constructor prevents this
                    raise AssertionError("QUOTE event missing observation")
                if observation.instrument_id != self._instrument_id:
                    reasons.add("INSTRUMENT_MISMATCH")
                fingerprint = self._observation_fingerprint(observation, event.schema_version)
                if not reasons and fingerprint in seen_observations:
                    duplicate_observations += 1
                    reasons.add("DUPLICATE_OBSERVATION")
                elif not reasons:
                    seen_observations.add(fingerprint)
                    source_ts = observation.source_ts.astimezone(UTC)
                    if watermark is not None and source_ts < watermark:
                        out_of_order += 1
                        reasons.add("OUT_OF_ORDER_OBSERVATION")
                        if source_ts < watermark - self._policy.allowed_lateness:
                            reasons.add("BEHIND_WATERMARK")
                    if not reasons:
                        latest_by_source[observation.source] = observation
                        watermark = max(watermark, source_ts) if watermark else source_ts
                        last_observation_at = event.first_seen_at
                        accepted_quotes += 1
                        state = CollectorState.STREAMING
        return (
            state,
            next_retry_at,
            attempts,
            watermark,
            last_observation_at,
            accepted_quotes,
            duplicate_observations,
            out_of_order,
        )

    def _apply_freshness(
        self,
        state: CollectorState,
        now: datetime,
        last_observation_at: datetime | None,
    ) -> tuple[CollectorState, str | None]:
        if state not in {CollectorState.STREAMING, CollectorState.DEGRADED}:
            return state, None
        if last_observation_at is None or now - last_observation_at > self._policy.freshness_limit:
            return CollectorState.DEGRADED, "COLLECTOR_STALE"
        return state, None

    @staticmethod
    def _observation_fingerprint(observation: QuoteObservation, schema_version: str) -> str:
        return freeze_snapshot(
            {
                "source": observation.source,
                "instrument_id": observation.instrument_id,
                "source_ts": observation.source_ts,
                "received_ts": observation.received_ts,
                "schema_version": schema_version,
                "bid": observation.bid,
                "ask": observation.ask,
                "bid_size": observation.bid_size,
                "ask_size": observation.ask_size,
                "field_timestamps": dict(observation.field_timestamps),
            }
        ).snapshot_id
