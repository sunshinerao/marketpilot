from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketpilot.services.collector_router import create_collector_router

BASE = datetime(2026, 8, 18, 14, 0, tzinfo=UTC)  # 10:00 America/New_York


def client() -> TestClient:
    app = FastAPI()
    app.include_router(create_collector_router())
    return TestClient(app)


def event(
    event_id: str,
    kind: str,
    offset: float,
    *,
    schema_version: str = "quotes-v1",
    **extra: object,
) -> dict[str, object]:
    timestamp = BASE + timedelta(seconds=offset)
    return {
        "event_id": event_id,
        "kind": kind,
        "published_at": timestamp.isoformat(),
        "first_seen_at": timestamp.isoformat(),
        "schema_version": schema_version,
        **extra,
    }


def quote_event(
    event_id: str,
    source: str,
    offset: float,
    *,
    quote_offset: float | None = None,
    instrument_id: str = "SPX",
    schema_version: str = "quotes-v1",
) -> dict[str, object]:
    source_offset = offset if quote_offset is None else quote_offset
    source_ts = BASE + timedelta(seconds=source_offset)
    received_ts = source_ts + timedelta(milliseconds=50)
    return event(
        event_id,
        "QUOTE",
        offset + 0.1,
        schema_version=schema_version,
        observation={
            "source": source,
            "instrument_id": instrument_id,
            "source_ts": source_ts.isoformat(),
            "received_ts": received_ts.isoformat(),
            "delayed": False,
            "entitlement": "VERIFIED",
            "bid": "6400.00",
            "ask": "6400.25",
            "bid_size": "10",
            "ask_size": "12",
            "field_timestamps": {
                "bid": source_ts.isoformat(),
                "ask": source_ts.isoformat(),
                "bid_size": source_ts.isoformat(),
                "ask_size": source_ts.isoformat(),
            },
        },
    )


def payload(events: list[dict[str, object]]) -> dict[str, object]:
    return {
        "run_mode": "SCENARIO",
        "provider": "licensed-scenario",
        "provider_version": "fixture-1",
        "expected_schema_version": "quotes-v1",
        "capability": {
            "provider": "licensed-scenario",
            "configured": True,
            "quality": "GREEN",
            "verification_status": "VERIFIED",
            "production_ready": True,
        },
        "session": {
            "kind": "EQUITY",
            "instrument_id": "SPX",
            "verified_from": "2026-01-01",
            "verified_through": "2026-12-31",
        },
        "policy": {
            "base_backoff_seconds": 1,
            "max_backoff_seconds": 4,
            "max_reconnect_attempts": 3,
            "allowed_lateness_seconds": 0.25,
            "freshness_limit_seconds": 5,
            "green_max_age_seconds": 2,
            "amber_max_age_seconds": 5,
            "max_receive_latency_seconds": 1,
            "conflict_absolute_tolerance": "0.50",
            "require_two_sources": True,
        },
        "events": events,
    }


def test_healthy_scenario_is_point_in_time_but_never_enables_execution() -> None:
    request = payload(
        [
            event("start", "START", 0),
            event("connected", "CONNECTED", 0.1),
            quote_event("quote-a", "licensed-a", 0.2),
            quote_event("quote-b", "licensed-b", 0.3),
        ]
    )

    response = client().post("/v1/scenario/collector/run", json=request)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "STREAMING"
    assert body["permits_decision"] is True
    assert body["execution_enabled"] is False
    assert body["action"] == "NO_TRADE"
    assert body["verification"] == "UNVERIFIED"
    assert body["accepted_quotes"] == 2
    assert body["reasons"] == []
    assert len(body["records"]) == len(request["events"]) * 2
    assert all(item["record_id"].startswith("sha256:") for item in body["records"])
    assert all(
        body["records"][index * 2]["record_id"] == trace["input_record_id"]
        and body["records"][index * 2 + 1]["record_id"] == trace["output_record_id"]
        for index, trace in enumerate(body["traces"])
    )
    repeated = client().post("/v1/scenario/collector/run", json=request).json()
    assert repeated["records"] == body["records"]


def test_disconnect_enforces_backoff_and_rejects_early_reconnect() -> None:
    request = payload(
        [
            event("start", "START", 0),
            event("connected", "CONNECTED", 0.1),
            event("lost", "CONNECTION_LOST", 1),
            event("too-early", "CONNECTED", 1.5),
            event("retry", "CONNECTED", 2),
            quote_event("quote-a", "licensed-a", 2.1),
            quote_event("quote-b", "licensed-b", 2.2),
        ]
    )

    body = client().post("/v1/scenario/collector/run", json=request).json()

    assert body["traces"][2]["state"] == "BACKING_OFF"
    assert datetime.fromisoformat(body["traces"][2]["next_retry_at"]) == BASE + timedelta(
        seconds=2
    )
    assert body["traces"][3]["accepted"] is False
    assert body["traces"][3]["reasons"] == ["RETRY_TOO_EARLY"]
    assert body["traces"][4]["state"] == "DEGRADED"
    assert body["traces"][4]["freeze"] is True
    assert body["state"] == "STREAMING"


def test_rate_limit_retry_after_and_freshness_are_fail_closed() -> None:
    request = payload(
        [
            event("start", "START", 0),
            event("connected", "CONNECTED", 0.1),
            quote_event("quote-a", "licensed-a", 0.2),
            quote_event("quote-b", "licensed-b", 0.3),
            event("limited", "RATE_LIMITED", 1, retry_after_seconds=7),
        ]
    )
    body = client().post("/v1/scenario/collector/run", json=request).json()

    assert body["state"] == "RATE_LIMITED"
    assert datetime.fromisoformat(body["next_retry_at"]) == BASE + timedelta(seconds=8)
    assert body["permits_decision"] is False
    assert "COLLECTOR_RATE_LIMITED" in body["reasons"]

    stale_request = payload(
        [
            event("start", "START", 0),
            event("connected", "CONNECTED", 0.1),
            quote_event("quote-a", "licensed-a", 0.2),
            quote_event("quote-b", "licensed-b", 0.3),
            event("heartbeat", "HEARTBEAT", 8),
        ]
    )
    stale = client().post("/v1/scenario/collector/run", json=stale_request).json()
    assert stale["state"] == "DEGRADED"
    assert stale["traces"][-1]["reasons"] == ["COLLECTOR_STALE"]
    assert stale["permits_decision"] is False


def test_schema_drift_halts_and_subsequent_events_cannot_recover() -> None:
    request = payload(
        [
            event("start", "START", 0),
            event("connected", "CONNECTED", 0.1),
            quote_event("drift", "licensed-a", 0.2, schema_version="quotes-v2"),
            event("later", "CONNECTED", 1),
        ]
    )

    body = client().post("/v1/scenario/collector/run", json=request).json()

    assert body["state"] == "HALTED"
    assert body["traces"][2]["reasons"] == ["SCHEMA_DRIFT"]
    assert body["traces"][3]["reasons"] == ["COLLECTOR_HALTED"]
    assert "SCHEMA_DRIFT" in body["reasons"]
    assert body["execution_enabled"] is False

    explicit = payload(
        [
            event("start", "START", 0),
            event("connected", "CONNECTED", 0.1),
            event(
                "provider-drift",
                "SCHEMA_DRIFT",
                0.2,
                observed_schema_version="quotes-v2",
            ),
        ]
    )
    explicit_body = client().post("/v1/scenario/collector/run", json=explicit).json()
    assert explicit_body["state"] == "HALTED"
    assert "SCHEMA_DRIFT" in explicit_body["reasons"]


def test_duplicate_and_out_of_order_observations_do_not_advance_watermark() -> None:
    repeated = quote_event("quote-a", "licensed-a", 1)
    duplicate = quote_event("quote-a-copy", "licensed-a", 1)
    late = quote_event("late", "licensed-a", 2, quote_offset=0)
    request = payload(
        [
            event("start", "START", 0),
            event("connected", "CONNECTED", 0.1),
            repeated,
            repeated,
            duplicate,
            late,
        ]
    )

    body = client().post("/v1/scenario/collector/run", json=request).json()

    assert body["accepted_quotes"] == 1
    assert body["duplicate_events"] == 1
    assert body["duplicate_observations"] == 1
    assert body["out_of_order_observations"] == 1
    assert datetime.fromisoformat(body["watermark"]) == BASE + timedelta(seconds=1)
    assert body["traces"][3]["reasons"] == ["DUPLICATE_EVENT"]
    assert body["traces"][4]["reasons"] == ["DUPLICATE_OBSERVATION"]
    assert body["traces"][5]["reasons"] == [
        "BEHIND_WATERMARK",
        "OUT_OF_ORDER_OBSERVATION",
    ]


def test_capability_session_and_instrument_gates_fail_closed() -> None:
    request = payload(
        [
            event("start", "START", 0),
            event("connected", "CONNECTED", 0.1),
            quote_event("wrong", "licensed-a", 0.2, instrument_id="ESU6"),
        ]
    )
    capability = request["capability"]
    assert isinstance(capability, dict)
    capability["production_ready"] = False
    session = request["session"]
    assert isinstance(session, dict)
    session["holidays"] = ["2026-08-18"]

    body = client().post("/v1/scenario/collector/run", json=request).json()

    assert body["traces"][-1]["reasons"] == ["COLLECTOR_STALE", "INSTRUMENT_MISMATCH"]
    assert "CAPABILITY_NOT_PRODUCTION_READY" in body["reasons"]
    assert "EQUITY_SESSION_HOLIDAY" in body["reasons"]
    assert body["permits_decision"] is False


def test_es_requires_explicit_contract_and_honors_globex_maintenance() -> None:
    request = payload([event("start", "START", 0)])
    request["session"] = {
        "kind": "ES",
        "symbol": "ES1!",
        "expiry": "2026-09-18",
        "verified_from": "2026-01-01",
        "verified_through": "2026-12-31",
    }
    invalid = client().post("/v1/scenario/collector/run", json=request)
    assert invalid.status_code == 422
    assert "continuous/main ES contracts are forbidden" in invalid.text

    request["session"] = {
        "kind": "ES",
        "symbol": "ESU6",
        "expiry": "2026-09-18",
        "verified_from": "2026-01-01",
        "verified_through": "2026-12-31",
    }
    request["events"] = [
        event("start", "START", 0),
        event("connected", "CONNECTED", 0.1),
        quote_event("quote-a", "licensed-a", 0.2, instrument_id="ESU6"),
        quote_event("quote-b", "licensed-b", 0.3, instrument_id="ESU6"),
    ]
    # 21:00 UTC is 17:00 ET in August, the Globex maintenance break.
    for item in request["events"]:
        item["published_at"] = item["first_seen_at"] = "2026-08-18T21:30:00+00:00"
    maintenance = client().post("/v1/scenario/collector/run", json=request).json()
    assert "GLOBEX_SESSION_MAINTENANCE" in maintenance["reasons"]
    assert maintenance["permits_decision"] is False


def test_live_mode_naive_times_and_future_publication_are_rejected() -> None:
    request = payload([event("start", "START", 0)])
    request["run_mode"] = "LIVE"
    assert client().post("/v1/scenario/collector/run", json=request).status_code == 422

    request["run_mode"] = "LOCAL"
    events = request["events"]
    assert isinstance(events, list)
    events[0]["published_at"] = "2026-08-18T14:00:01+00:00"
    events[0]["first_seen_at"] = "2026-08-18T14:00:00+00:00"
    response = client().post("/v1/scenario/collector/run", json=request)
    assert response.status_code == 422
    assert "published_at must be less than or equal to first_seen_at" in response.text

    events[0]["published_at"] = "2026-08-18T14:00:00"
    events[0]["first_seen_at"] = "2026-08-18T14:00:00"
    response = client().post("/v1/scenario/collector/run", json=request)
    assert response.status_code == 422
    assert "timezone-aware" in response.text


def test_reconnect_budget_and_event_time_regression_halt_or_reject_safely() -> None:
    request = payload(
        [
            event("start", "START", 0),
            event("connected", "CONNECTED", 0.1),
            event("lost-1", "CONNECTION_LOST", 1),
            event("lost-2", "CONNECTION_LOST", 2),
            event("lost-3", "CONNECTION_LOST", 3),
            event("lost-4", "CONNECTION_LOST", 4),
        ]
    )
    exhausted = client().post("/v1/scenario/collector/run", json=request).json()
    assert exhausted["state"] == "HALTED"
    assert exhausted["traces"][2]["next_retry_at"] == "2026-08-18T14:00:02Z"
    assert exhausted["traces"][3]["next_retry_at"] == "2026-08-18T14:00:04Z"
    assert exhausted["traces"][4]["next_retry_at"] == "2026-08-18T14:00:07Z"
    assert exhausted["traces"][5]["reasons"] == ["RECONNECT_BUDGET_EXHAUSTED"]

    regression = payload(
        [
            event("start", "START", 1),
            event("connected", "CONNECTED", 0),
        ]
    )
    regressed = client().post("/v1/scenario/collector/run", json=regression).json()
    assert regressed["traces"][1]["accepted"] is False
    assert regressed["traces"][1]["reasons"] == ["EVENT_TIME_REGRESSION"]
