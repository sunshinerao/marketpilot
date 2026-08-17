from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from marketpilot.domain.data_quality import (
    EntitlementStatus,
    QualityPolicy,
    QuoteObservation,
    QuoteQualityEvaluator,
)
from marketpilot.domain.market import DataQuality

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def policy(*, require_two_sources: bool = True) -> QualityPolicy:
    return QualityPolicy(
        green_max_age=timedelta(seconds=2),
        amber_max_age=timedelta(seconds=5),
        max_receive_latency=timedelta(seconds=1),
        conflict_absolute_tolerance=Decimal("0.50"),
        conflict_relative_tolerance=Decimal("0.0001"),
        require_two_sources=require_two_sources,
    )


def quote(
    source: str,
    *,
    mid: Decimal = Decimal("6400"),
    age_seconds: float = 1,
    delayed: bool | None = False,
    entitlement: EntitlementStatus = EntitlementStatus.VERIFIED,
) -> QuoteObservation:
    timestamp = NOW - timedelta(seconds=age_seconds)
    return QuoteObservation(
        source=source,
        instrument_id="ESU6@XCME",
        source_ts=timestamp,
        received_ts=timestamp + timedelta(milliseconds=100),
        delayed=delayed,
        entitlement=entitlement,
        bid=mid - Decimal("0.125"),
        ask=mid + Decimal("0.125"),
        bid_size=Decimal("10"),
        ask_size=Decimal("12"),
        field_timestamps={
            "bid": timestamp,
            "ask": timestamp,
            "bid_size": timestamp,
            "ask_size": timestamp,
        },
    )


def test_fresh_entitled_two_source_quote_is_green() -> None:
    report = QuoteQualityEvaluator(policy()).evaluate(
        [quote("licensed-a"), quote("licensed-b", mid=Decimal("6400.25"))],
        as_of=NOW,
    )

    assert report.status is DataQuality.GREEN
    assert report.freeze is False
    assert report.permits_decision is True
    assert report.reasons == ()


def test_moderately_stale_fields_are_amber_and_fail_decision_gate() -> None:
    report = QuoteQualityEvaluator(policy()).evaluate(
        [quote("licensed-a", age_seconds=3), quote("licensed-b", age_seconds=3)],
        as_of=NOW,
    )

    assert report.status is DataQuality.AMBER
    assert report.freeze is False
    assert report.permits_decision is False
    assert "licensed-a:bid" in report.stale_fields


def test_missing_source_is_red_and_fail_closed() -> None:
    report = QuoteQualityEvaluator(policy()).evaluate([quote("only-source")], as_of=NOW)

    assert report.status is DataQuality.RED
    assert report.freeze is True
    assert report.reasons == ("SECOND_SOURCE_MISSING",)


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        (quote("a", delayed=True), "a:DELAYED"),
        (quote("a", delayed=None), "a:DELAY_STATUS_UNKNOWN"),
        (
            quote("a", entitlement=EntitlementStatus.UNKNOWN),
            "a:ENTITLEMENT_UNKNOWN",
        ),
        (quote("a", age_seconds=8), "a:OBSERVATION_STALE"),
    ],
)
def test_delay_entitlement_and_staleness_fail_closed(
    observation: QuoteObservation,
    reason: str,
) -> None:
    report = QuoteQualityEvaluator(policy(require_two_sources=False)).evaluate(
        [observation], as_of=NOW
    )

    assert report.status is DataQuality.RED
    assert report.freeze is True
    assert reason in report.reasons


def test_missing_quote_sizes_or_field_timestamps_are_red() -> None:
    item = quote("a")
    incomplete = QuoteObservation(
        source=item.source,
        instrument_id=item.instrument_id,
        source_ts=item.source_ts,
        received_ts=item.received_ts,
        delayed=item.delayed,
        entitlement=item.entitlement,
        bid=item.bid,
        ask=item.ask,
        bid_size=None,
        ask_size=item.ask_size,
        field_timestamps={"bid": item.source_ts, "ask": item.source_ts},
    )
    report = QuoteQualityEvaluator(policy(require_two_sources=False)).evaluate(
        [incomplete], as_of=NOW
    )

    assert report.status is DataQuality.RED
    assert "a:MISSING_BID_SIZE" in report.reasons
    assert "a:MISSING_ASK_SIZE_TIMESTAMP" in report.reasons


def test_dual_source_conflict_freezes_the_system() -> None:
    report = QuoteQualityEvaluator(policy()).evaluate(
        [quote("a", mid=Decimal("6400")), quote("b", mid=Decimal("6402"))],
        as_of=NOW,
    )

    assert report.status is DataQuality.RED
    assert report.freeze is True
    assert report.reasons == ("DUAL_SOURCE_CONFLICT",)


def test_future_timestamp_and_crossed_quote_are_red() -> None:
    item = quote("a", age_seconds=-1)
    broken = QuoteObservation(
        source=item.source,
        instrument_id=item.instrument_id,
        source_ts=item.source_ts,
        received_ts=item.received_ts,
        delayed=item.delayed,
        entitlement=item.entitlement,
        bid=Decimal("6401"),
        ask=Decimal("6400"),
        bid_size=item.bid_size,
        ask_size=item.ask_size,
        field_timestamps=item.field_timestamps,
    )
    report = QuoteQualityEvaluator(policy(require_two_sources=False)).evaluate([broken], as_of=NOW)

    assert report.status is DataQuality.RED
    assert "a:CROSSED_QUOTE" in report.reasons
    assert any("FUTURE_TIMESTAMP" in reason for reason in report.reasons)


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValueError, match="source_ts must be timezone-aware"):
        QuoteObservation(
            source="a",
            instrument_id="ESU6@XCME",
            source_ts=datetime(2026, 8, 16, 12),
            received_ts=NOW,
            delayed=False,
            entitlement=EntitlementStatus.VERIFIED,
            bid=Decimal("1"),
            ask=Decimal("2"),
            bid_size=Decimal("1"),
            ask_size=Decimal("1"),
        )
