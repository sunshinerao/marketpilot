from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from time import perf_counter
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from marketpilot.domain.capabilities import (
    CapabilityReport,
    CapabilityResult,
    CapabilityStatus,
    LatencySummary,
)
from marketpilot.domain.contracts import normalize_explicit_es_symbol
from marketpilot.domain.market import DataQuality

FUTURES_CATEGORY = "US_FUTURES"
OPTION_CATEGORY = "US_OPTION"


class WebullSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WEBULL_",
        extra="ignore",
        frozen=True,
    )

    app_key: SecretStr | None = None
    app_secret: SecretStr | None = None
    region: str = "us"
    api_endpoint: str | None = None
    es_contract: str | None = None
    spxw_expiration: str | None = None
    spxw_option_symbol: str | None = None
    token_dir: str | None = None

    @field_validator("es_contract")
    @classmethod
    def require_explicit_es_contract(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized:
            return None
        try:
            return normalize_explicit_es_symbol(normalized)
        except ValueError as exc:
            raise ValueError("WEBULL_ES_CONTRACT must be an explicit dated contract") from exc

    @property
    def has_credentials(self) -> bool:
        return self.app_key is not None and self.app_secret is not None


class PayloadEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    status_code: int
    payload: Any


class WebullGateway(Protocol):
    def futures_instrument(self, symbol: str) -> PayloadEnvelope: ...

    def futures_snapshot(self, symbol: str) -> PayloadEnvelope: ...

    def futures_history_m1(self, symbol: str) -> PayloadEnvelope: ...

    def spxw_contracts(self, expiration: str | None) -> PayloadEnvelope: ...

    def option_snapshot(self, symbol: str) -> PayloadEnvelope: ...

    def option_history_m1(self, symbol: str) -> PayloadEnvelope: ...


class WebullSdkGateway:
    def __init__(self, settings: WebullSettings) -> None:
        if not settings.has_credentials:
            raise ValueError("Webull credentials are not configured")

        from webull.core.client import ApiClient  # type: ignore[import-untyped]
        from webull.data.data_client import DataClient  # type: ignore[import-untyped]

        app_key = cast(SecretStr, settings.app_key).get_secret_value()
        app_secret = cast(SecretStr, settings.app_secret).get_secret_value()
        api_client = ApiClient(
            app_key,
            app_secret,
            settings.region,
            connect_timeout=5,
            timeout=10,
        )
        # Prevent the SDK default from creating a local log containing response bodies.
        api_client.set_logger(logging.getLogger("marketpilot.webull"))
        api_client._stream_logger_set = True  # noqa: SLF001
        if settings.api_endpoint:
            api_client.add_endpoint(settings.region, settings.api_endpoint)
        if settings.token_dir:
            api_client.set_token_dir(settings.token_dir)
        self._client = DataClient(api_client)

    @staticmethod
    def _envelope(response: Any) -> PayloadEnvelope:
        return PayloadEnvelope(status_code=int(response.status_code), payload=response.json())

    def futures_instrument(self, symbol: str) -> PayloadEnvelope:
        response = self._client.instrument.get_futures_instrument(
            symbols=symbol,
            category=FUTURES_CATEGORY,
        )
        return self._envelope(response)

    def futures_snapshot(self, symbol: str) -> PayloadEnvelope:
        response = self._client.futures_market_data.get_futures_snapshot(
            symbols=symbol,
            category=FUTURES_CATEGORY,
        )
        return self._envelope(response)

    def futures_history_m1(self, symbol: str) -> PayloadEnvelope:
        response = self._client.futures_market_data.get_futures_history_bars(
            symbols=symbol,
            category=FUTURES_CATEGORY,
            timespan="m1",
            count="30",
        )
        return self._envelope(response)

    def spxw_contracts(self, expiration: str | None) -> PayloadEnvelope:
        response = self._client.instrument.get_option_contracts(
            category=OPTION_CATEGORY,
            root_symbol="SPXW",
            start_date=expiration,
            end_date=expiration,
            page_size=10,
        )
        return self._envelope(response)

    def option_snapshot(self, symbol: str) -> PayloadEnvelope:
        response = self._client.option_market_data.get_option_snapshot(
            symbols=symbol,
            category=OPTION_CATEGORY,
        )
        return self._envelope(response)

    def option_history_m1(self, symbol: str) -> PayloadEnvelope:
        response = self._client.option_market_data.get_option_history_bars(
            symbols=symbol,
            category=OPTION_CATEGORY,
            timespan="m1",
            count="30",
        )
        return self._envelope(response)


GatewayFactory = Callable[[WebullSettings], WebullGateway]


@dataclass(frozen=True, slots=True)
class _ProbeDefinition:
    capability_id: str
    call: Callable[[], PayloadEnvelope]


def _sdk_version() -> str:
    try:
        return version("webull-openapi-python-sdk")
    except PackageNotFoundError:
        return "not-installed"


def _field_paths(value: Any, prefix: str = "", limit: int = 80) -> tuple[str, ...]:
    paths: set[str] = set()

    def visit(item: Any, current: str) -> None:
        if len(paths) >= limit:
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                path = f"{current}.{key}" if current else str(key)
                paths.add(path)
                visit(child, path)
        elif isinstance(item, list) and item:
            path = f"{current}[]"
            paths.add(path)
            visit(item[0], path)

    visit(value, prefix)
    return tuple(sorted(paths))


def _has_payload(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (Mapping, list, tuple, str)):
        return bool(value)
    return True


def _latency(elapsed_ms: float) -> LatencySummary:
    rounded = round(elapsed_ms, 2)
    return LatencySummary(samples=1, p50_ms=rounded, p95_ms=rounded, p99_ms=rounded)


class WebullCapabilityProbe:
    def __init__(
        self,
        settings: WebullSettings,
        gateway_factory: GatewayFactory = WebullSdkGateway,
    ) -> None:
        self._settings = settings
        self._gateway_factory = gateway_factory

    def run(self) -> CapabilityReport:
        checked_at = datetime.now(UTC)
        results: list[CapabilityResult] = []
        if not self._settings.has_credentials:
            results.append(
                CapabilityResult(
                    capability_id="credentials",
                    status=CapabilityStatus.FAIL,
                    checked_at=checked_at,
                    message="WEBULL_APP_KEY and WEBULL_APP_SECRET are required",
                )
            )
            return self._report(checked_at, results)

        results.append(
            CapabilityResult(
                capability_id="credentials",
                status=CapabilityStatus.PASS,
                checked_at=checked_at,
                message="Credentials are configured; values were not persisted",
            )
        )
        try:
            gateway = self._gateway_factory(self._settings)
        # SDK exceptions may include request details; never persist str(exc).
        except Exception as exc:
            results.append(self._safe_error("sdk_initialization", exc, checked_at))
            return self._report(checked_at, results)

        definitions: list[_ProbeDefinition] = []
        if self._settings.es_contract:
            symbol = self._settings.es_contract
            definitions.extend(
                [
                    _ProbeDefinition(
                        "es_explicit_contract", lambda: gateway.futures_instrument(symbol)
                    ),
                    _ProbeDefinition("es_snapshot", lambda: gateway.futures_snapshot(symbol)),
                    _ProbeDefinition("es_history_m1", lambda: gateway.futures_history_m1(symbol)),
                ]
            )
        else:
            results.append(self._skipped("es_explicit_contract", "WEBULL_ES_CONTRACT is not set"))
            results.append(self._skipped("es_snapshot", "Explicit ES contract is required"))
            results.append(self._skipped("es_history_m1", "Explicit ES contract is required"))

        definitions.append(
            _ProbeDefinition(
                "spxw_contract_discovery",
                lambda: gateway.spxw_contracts(self._settings.spxw_expiration),
            )
        )
        if self._settings.spxw_option_symbol:
            option_symbol = self._settings.spxw_option_symbol
            definitions.extend(
                [
                    _ProbeDefinition(
                        "spxw_option_snapshot", lambda: gateway.option_snapshot(option_symbol)
                    ),
                    _ProbeDefinition(
                        "spxw_option_history_m1", lambda: gateway.option_history_m1(option_symbol)
                    ),
                ]
            )
        else:
            results.append(
                self._skipped("spxw_option_snapshot", "WEBULL_SPXW_OPTION_SYMBOL is not set")
            )
            results.append(
                self._skipped("spxw_option_history_m1", "WEBULL_SPXW_OPTION_SYMBOL is not set")
            )

        results.extend(self._execute(item) for item in definitions)
        results.sort(key=lambda item: item.capability_id)
        return self._report(checked_at, results)

    @staticmethod
    def _execute(definition: _ProbeDefinition) -> CapabilityResult:
        checked_at = datetime.now(UTC)
        started = perf_counter()
        try:
            envelope = definition.call()
        except Exception as exc:
            return WebullCapabilityProbe._safe_error(
                definition.capability_id,
                exc,
                checked_at,
                (perf_counter() - started) * 1000,
            )
        elapsed_ms = (perf_counter() - started) * 1000
        passed = 200 <= envelope.status_code < 300 and _has_payload(envelope.payload)
        return CapabilityResult(
            capability_id=definition.capability_id,
            status=CapabilityStatus.PASS if passed else CapabilityStatus.FAIL,
            checked_at=checked_at,
            message="Response schema observed" if passed else "Empty or non-success response",
            http_status=envelope.status_code,
            latency=_latency(elapsed_ms),
            field_paths=_field_paths(envelope.payload),
        )

    @staticmethod
    def _safe_error(
        capability_id: str,
        exc: Exception,
        checked_at: datetime,
        elapsed_ms: float | None = None,
    ) -> CapabilityResult:
        return CapabilityResult(
            capability_id=capability_id,
            status=CapabilityStatus.ERROR,
            checked_at=checked_at,
            message=f"Provider call failed ({type(exc).__name__}); details omitted",
            latency=_latency(elapsed_ms) if elapsed_ms is not None else LatencySummary(),
        )

    @staticmethod
    def _skipped(capability_id: str, message: str) -> CapabilityResult:
        return CapabilityResult(
            capability_id=capability_id,
            status=CapabilityStatus.SKIPPED,
            checked_at=datetime.now(UTC),
            message=message,
        )

    def _report(
        self,
        probed_at: datetime,
        results: list[CapabilityResult],
    ) -> CapabilityReport:
        passed = sum(result.status is CapabilityStatus.PASS for result in results)
        failed = any(
            result.status in {CapabilityStatus.FAIL, CapabilityStatus.ERROR} for result in results
        )
        complete = bool(results) and all(
            result.status is CapabilityStatus.PASS for result in results
        )
        quality = (
            DataQuality.GREEN
            if complete
            else DataQuality.AMBER
            if passed and not failed
            else DataQuality.RED
        )
        return CapabilityReport(
            probed_at=probed_at,
            sdk_version=_sdk_version(),
            environment=self._settings.region,
            configured=self._settings.has_credentials,
            quality=quality,
            results=tuple(results),
        )
