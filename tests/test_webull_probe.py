from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from marketpilot.adapters.webull import (
    PayloadEnvelope,
    WebullCapabilityProbe,
    WebullSettings,
)
from marketpilot.domain.capabilities import CapabilityReport, CapabilityResult, CapabilityStatus
from marketpilot.domain.market import DataQuality
from marketpilot.services.capability_store import CapabilityReportStore


class FakeGateway:
    def futures_instrument(self, symbol: str) -> PayloadEnvelope:
        return PayloadEnvelope(
            status_code=200,
            payload={"items": [{"symbol": symbol, "expiry": "2026-09"}]},
        )

    def futures_snapshot(self, symbol: str) -> PayloadEnvelope:
        return PayloadEnvelope(
            status_code=200,
            payload={"symbol": symbol, "bid": "6400.00", "ask": "6400.25"},
        )

    def futures_history_m1(self, symbol: str) -> PayloadEnvelope:
        return PayloadEnvelope(status_code=200, payload=[{"symbol": symbol, "timestamp": 1}])

    def spxw_contracts(self, expiration: str | None) -> PayloadEnvelope:
        return PayloadEnvelope(
            status_code=200,
            payload={"items": [{"root_symbol": "SPXW", "expiration": expiration}]},
        )

    def option_snapshot(self, symbol: str) -> PayloadEnvelope:
        return PayloadEnvelope(status_code=200, payload={"symbol": symbol, "bid": "5.10"})

    def option_history_m1(self, symbol: str) -> PayloadEnvelope:
        return PayloadEnvelope(status_code=200, payload=[{"symbol": symbol, "close": "5.20"}])


def configured_settings() -> WebullSettings:
    return WebullSettings(
        app_key="test-app-key",
        app_secret="test-app-secret",
        es_contract="ESU6",
        spxw_expiration="2026-08-17",
        spxw_option_symbol="SPXW260817C06400000",
    )


def test_missing_credentials_fail_closed_without_gateway_call() -> None:
    report = WebullCapabilityProbe(
        WebullSettings(app_key=None, app_secret=None),
        gateway_factory=lambda _: pytest.fail("gateway must not be created"),
    ).run()

    assert report.quality is DataQuality.RED
    assert report.configured is False
    assert report.results[0].capability_id == "credentials"
    assert report.results[0].status is CapabilityStatus.FAIL


def test_continuous_contract_is_rejected_before_network() -> None:
    with pytest.raises(ValidationError, match="explicit dated contract"):
        WebullSettings(es_contract="ESmain")


def test_successful_probe_records_schema_only_and_is_green() -> None:
    report = WebullCapabilityProbe(configured_settings(), lambda _: FakeGateway()).run()

    assert report.quality is DataQuality.GREEN
    assert report.verification_status == "SCHEMA_ONLY"
    assert report.production_ready is False
    assert "ACCOUNT_ENTITLEMENT" in report.unverified_requirements
    assert all(result.status is CapabilityStatus.PASS for result in report.results)
    snapshot = next(item for item in report.results if item.capability_id == "es_snapshot")
    assert "bid" in snapshot.field_paths
    serialized = report.model_dump_json()
    assert "test-app-key" not in serialized
    assert "test-app-secret" not in serialized
    assert "6400.00" not in serialized


def test_partial_probe_is_amber_when_only_optional_symbols_are_missing() -> None:
    settings = configured_settings().model_copy(update={"spxw_option_symbol": None})
    report = WebullCapabilityProbe(settings, lambda _: FakeGateway()).run()

    assert report.quality is DataQuality.AMBER
    assert any(item.status is CapabilityStatus.SKIPPED for item in report.results)


def test_provider_errors_omit_sensitive_exception_details() -> None:
    class BrokenGateway(FakeGateway):
        def futures_snapshot(self, symbol: str) -> PayloadEnvelope:
            raise RuntimeError("test-app-secret leaked provider payload")

    report = WebullCapabilityProbe(configured_settings(), lambda _: BrokenGateway()).run()
    serialized = report.model_dump_json()

    assert report.quality is DataQuality.RED
    assert "leaked provider payload" not in serialized
    assert "RuntimeError" in serialized


def test_capability_store_writes_versioned_and_latest_reports(tmp_path: Path) -> None:
    store = CapabilityReportStore(tmp_path)
    report = CapabilityReport(
        probed_at=datetime(2026, 8, 16, tzinfo=UTC),
        sdk_version="2.0.14",
        environment="us",
        configured=False,
        quality=DataQuality.RED,
        results=(
            CapabilityResult(
                capability_id="credentials",
                status=CapabilityStatus.FAIL,
                checked_at=datetime(2026, 8, 16, tzinfo=UTC),
                message="not configured",
            ),
        ),
    )

    path = store.save(report)
    loaded = store.latest("webull")

    assert path.exists()
    assert loaded == report
    assert json.loads(path.read_text())["quality"] == "RED"


def test_corrupt_latest_capability_report_fails_closed(tmp_path: Path) -> None:
    provider_dir = tmp_path / "webull"
    provider_dir.mkdir()
    (provider_dir / "latest.json").write_text('{"quality":', encoding="utf-8")

    assert CapabilityReportStore(tmp_path).latest("webull") is None
