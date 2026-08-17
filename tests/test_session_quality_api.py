from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketpilot.services.session_quality_router import create_session_quality_router


def client() -> TestClient:
    app = FastAPI()
    app.include_router(create_session_quality_router())
    return TestClient(app)


def equity_payload() -> dict[str, object]:
    return {
        "run_mode": "SCENARIO",
        "session_date": "2026-11-27",
        "verified_from": "2026-01-01",
        "verified_through": "2026-12-31",
        "holidays": ["2026-12-25"],
        "early_closes": [{"session_date": "2026-11-27", "closes_at": "13:00:00"}],
    }


def quote_payload() -> dict[str, object]:
    observed_at = datetime(2026, 8, 16, 12, tzinfo=UTC)

    def observation(source: str, bid: str, ask: str) -> dict[str, object]:
        timestamp = "2026-08-16T11:59:59+00:00"
        return {
            "source": source,
            "instrument_id": "ESU6@XCME",
            "source_ts": timestamp,
            "received_ts": "2026-08-16T11:59:59.100+00:00",
            "delayed": False,
            "entitlement": "VERIFIED",
            "bid": bid,
            "ask": ask,
            "bid_size": "10",
            "ask_size": "12",
            "field_timestamps": {
                "bid": timestamp,
                "ask": timestamp,
                "bid_size": timestamp,
                "ask_size": timestamp,
            },
        }

    return {
        "run_mode": "SCENARIO",
        "as_of": observed_at.isoformat(),
        "policy": {
            "green_max_age_seconds": 2,
            "amber_max_age_seconds": 5,
            "max_receive_latency_seconds": 1,
            "conflict_absolute_tolerance": "0.50",
            "conflict_relative_tolerance": "0.0001",
            "require_two_sources": True,
        },
        "observations": [
            observation("licensed-a", "6399.875", "6400.125"),
            observation("licensed-b", "6401.875", "6402.125"),
        ],
    }


def test_live_inputs_are_rejected_by_every_endpoint() -> None:
    api = client()
    equity = equity_payload()
    equity["run_mode"] = "LIVE"
    globex = {
        "run_mode": "LIVE",
        "instant": "2026-08-18T17:30:00-04:00",
        "verified_from": "2026-08-01",
        "verified_through": "2026-08-31",
    }
    quality = quote_payload()
    quality["run_mode"] = "LIVE"

    assert api.post("/v1/scenario/session-quality/equity-session", json=equity).status_code == 422
    assert api.post("/v1/scenario/session-quality/globex-session", json=globex).status_code == 422
    assert api.post("/v1/scenario/session-quality/quote-quality", json=quality).status_code == 422


def test_naive_times_are_rejected_at_the_api_boundary() -> None:
    api = client()
    globex = {
        "run_mode": "SCENARIO",
        "instant": "2026-08-18T17:30:00",
        "verified_from": "2026-08-01",
        "verified_through": "2026-08-31",
    }
    quality = quote_payload()
    quality["as_of"] = "2026-08-16T12:00:00"
    nested_quality = quote_payload()
    observations = nested_quality["observations"]
    assert isinstance(observations, list)
    first = observations[0]
    assert isinstance(first, dict)
    first["source_ts"] = "2026-08-16T11:59:59"

    globex_response = api.post("/v1/scenario/session-quality/globex-session", json=globex)
    quote_response = api.post("/v1/scenario/session-quality/quote-quality", json=quality)
    nested_response = api.post(
        "/v1/scenario/session-quality/quote-quality",
        json=nested_quality,
    )

    assert globex_response.status_code == 422
    assert "timezone-aware" in globex_response.text
    assert quote_response.status_code == 422
    assert "timezone-aware" in quote_response.text
    assert nested_response.status_code == 422
    assert "timezone-aware" in nested_response.text


def test_half_day_uses_actual_close_as_unverified_scenario_anchor() -> None:
    response = client().post(
        "/v1/scenario/session-quality/equity-session",
        json=equity_payload(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_mode"] == "SCENARIO"
    assert body["verification"] == "UNVERIFIED"
    assert body["execution_enabled"] is False
    assert body["action"] == "NO_TRADE"
    assert body["is_early_close"] is True
    assert body["closes_at"] == "2026-11-27T13:00:00-05:00"
    assert body["anchor_at"] == body["closes_at"]


def test_dual_source_conflict_is_red_frozen_and_unverified() -> None:
    response = client().post(
        "/v1/scenario/session-quality/quote-quality",
        json=quote_payload(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["verification"] == "UNVERIFIED"
    assert body["execution_enabled"] is False
    assert body["quality"] == "RED"
    assert body["freeze"] is True
    assert body["permits_decision"] is False
    assert body["reasons"] == ["DUAL_SOURCE_CONFLICT"]
