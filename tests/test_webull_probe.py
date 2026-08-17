from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from marketpilot.adapters.webull import (
    PayloadEnvelope,
    WebullCapabilityProbe,
    WebullSdkGateway,
    WebullSettings,
)
from marketpilot.domain.capabilities import (
    CapabilityReport,
    CapabilityResult,
    CapabilityStatus,
    CoverageConclusion,
)
from marketpilot.domain.market import DataQuality
from marketpilot.services.capability_store import CapabilityReportStore


class FakeGateway:
    def __init__(self, index_market_data: bool = False) -> None:
        self._index_market_data = index_market_data

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

    def app_subscriptions(self) -> PayloadEnvelope:
        return PayloadEnvelope(
            status_code=200,
            payload={"items": [{"subscription_name": "SECRET-PLAN-NAME", "status": "ACTIVE"}]},
        )

    def supports_index_market_data(self) -> bool:
        return self._index_market_data


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
    assert report.coverage_findings == ()


def test_continuous_contract_is_rejected_before_network() -> None:
    with pytest.raises(ValidationError, match="explicit dated contract"):
        WebullSettings(es_contract="ESmain")


def test_successful_probe_records_schema_only_and_is_green() -> None:
    report = WebullCapabilityProbe(configured_settings(), lambda _: FakeGateway()).run()

    assert report.quality is DataQuality.GREEN
    assert report.report_version == "2"
    assert report.verification_status == "SCHEMA_ONLY"
    assert report.production_ready is False
    assert "ACCOUNT_ENTITLEMENT" in report.unverified_requirements
    assert all(result.status is CapabilityStatus.PASS for result in report.results)
    snapshot = next(item for item in report.results if item.capability_id == "es_snapshot")
    assert "bid" in snapshot.field_paths
    subscriptions = next(
        item for item in report.results if item.capability_id == "account_subscriptions"
    )
    assert subscriptions.status is CapabilityStatus.PASS
    serialized = report.model_dump_json()
    assert "test-app-key" not in serialized
    assert "test-app-secret" not in serialized
    assert "6400.00" not in serialized
    assert "SECRET-PLAN-NAME" not in serialized


def test_coverage_findings_record_index_gap_and_manual_residue() -> None:
    report = WebullCapabilityProbe(configured_settings(), lambda _: FakeGateway()).run()

    findings = {item.finding_id: item for item in report.coverage_findings}
    assert set(findings) == {
        "SPX_INDEX_COVERAGE",
        "VIX_VIX1D_COVERAGE",
        "EXPIRED_SPXW_NBBO_DEPTH",
        "MARKET_DATA_ENTITLEMENT_SCOPE",
    }
    assert findings["SPX_INDEX_COVERAGE"].conclusion is CoverageConclusion.NOT_OFFERED
    assert findings["VIX_VIX1D_COVERAGE"].conclusion is CoverageConclusion.NOT_OFFERED
    assert "no index market-data module" in findings["SPX_INDEX_COVERAGE"].evidence
    assert "Databento" in findings["SPX_INDEX_COVERAGE"].required_action
    assert (
        findings["EXPIRED_SPXW_NBBO_DEPTH"].conclusion is CoverageConclusion.UNVERIFIED
    )
    assert (
        findings["MARKET_DATA_ENTITLEMENT_SCOPE"].conclusion
        is CoverageConclusion.UNVERIFIED
    )


def test_index_module_presence_downgrades_finding_to_unverified_not_offered() -> None:
    report = WebullCapabilityProbe(
        configured_settings(),
        lambda _: FakeGateway(index_market_data=True),
    ).run()

    findings = {item.finding_id: item for item in report.coverage_findings}
    assert findings["SPX_INDEX_COVERAGE"].conclusion is CoverageConclusion.UNVERIFIED
    assert findings["VIX_VIX1D_COVERAGE"].conclusion is CoverageConclusion.UNVERIFIED
    assert "no SPX/VIX probe is wired yet" in findings["SPX_INDEX_COVERAGE"].evidence


def test_entitlement_probe_error_omits_details_and_fails_closed() -> None:
    class BrokenGateway(FakeGateway):
        def app_subscriptions(self) -> PayloadEnvelope:
            raise RuntimeError("test-app-secret subscription payload")

    report = WebullCapabilityProbe(configured_settings(), lambda _: BrokenGateway()).run()
    serialized = report.model_dump_json()

    assert report.quality is DataQuality.RED
    subscriptions = next(
        item for item in report.results if item.capability_id == "account_subscriptions"
    )
    assert subscriptions.status is CapabilityStatus.ERROR
    assert "subscription payload" not in serialized


class _FakeSdkResponse:
    status_code = 200

    @staticmethod
    def json() -> dict[str, object]:
        return {"items": [{"subscription_name": "plan"}]}


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch, *, index_module: bool) -> None:
    class FakeApiClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def set_logger(self, logger: object) -> None:
            pass

    class FakeDataClient:
        def __init__(self, api_client: object) -> None:
            if index_module:
                self.index_market_data = object()

    class FakeAccount:
        def __init__(self, api_client: object) -> None:
            pass

        def get_app_subscriptions(self) -> _FakeSdkResponse:
            return _FakeSdkResponse()

    core_module = types.ModuleType("webull.core.client")
    core_module.ApiClient = FakeApiClient  # type: ignore[attr-defined]
    data_module = types.ModuleType("webull.data.data_client")
    data_module.DataClient = FakeDataClient  # type: ignore[attr-defined]
    trade_module = types.ModuleType("webull.trade.trade.account_info")
    trade_module.Account = FakeAccount  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webull.core.client", core_module)
    monkeypatch.setitem(sys.modules, "webull.data.data_client", data_module)
    monkeypatch.setitem(sys.modules, "webull.trade.trade.account_info", trade_module)


def test_sdk_gateway_wraps_subscriptions_without_order_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(monkeypatch, index_module=True)
    gateway = WebullSdkGateway(configured_settings())

    envelope = gateway.app_subscriptions()

    assert envelope.status_code == 200
    assert gateway.supports_index_market_data() is True


def test_sdk_gateway_reports_missing_index_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(monkeypatch, index_module=False)
    gateway = WebullSdkGateway(configured_settings())

    assert gateway.supports_index_market_data() is False


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
