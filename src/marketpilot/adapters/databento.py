from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from time import sleep
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

NEW_YORK = ZoneInfo("America/New_York")
SUPPORTED_STYPES = frozenset({"parent", "raw_symbol", "continuous"})


class DatabentoApiError(RuntimeError):
    """Provider error reduced to a status code and machine case; never echoes secrets."""

    def __init__(self, status_code: int, case: str) -> None:
        super().__init__(f"databento API error status={status_code} case={case}")
        self.status_code = status_code
        self.case = case


class DatabentoSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATABENTO_",
        extra="ignore",
        frozen=True,
    )

    api_key: SecretStr | None = None
    base_url: str = "https://hist.databento.com/v0"
    max_cost_usd: float = 25.0

    @property
    def has_credentials(self) -> bool:
        return self.api_key is not None


@dataclass(frozen=True, slots=True)
class DayPull:
    """One dataset/schema/symbol-selection/day batch request."""

    dataset: str
    schema: str
    day: date
    stype_in: str
    symbols: tuple[str, ...]
    scope: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("dataset", self.dataset),
            ("schema", self.schema),
            ("scope", self.scope),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.stype_in not in SUPPORTED_STYPES:
            raise ValueError(f"stype_in must be one of {sorted(SUPPORTED_STYPES)}")
        if not self.symbols or any(not symbol.strip() for symbol in self.symbols):
            raise ValueError("symbols must contain at least one non-blank symbol")

    @property
    def logical_key(self) -> str:
        return f"{self.dataset}/{self.schema}/{self.scope}/{self.day.isoformat()}"


class HistoricalGateway(Protocol):
    def estimate_cost(self, pull: DayPull) -> float: ...

    def record_count(self, pull: DayPull) -> int: ...

    def download_day(self, pull: DayPull) -> bytes: ...

    def download_definitions(self, day: date) -> bytes: ...


DEFINITIONS_DATASET = "OPRA.PILLAR"
DEFINITIONS_SCHEMA = "definition"
DEFINITIONS_SCOPE = "spxw-definitions"
SPXW_PARENT_SYMBOL = "SPXW.OPT"
# Databento's UNDEFINED sentinel for uint64 fields (verified live 2026-08-19).
UNDEFINED_TIMESTAMP_NS = 9_223_372_036_854_775_807


def spxw_definitions_pull(day: date) -> DayPull:
    """The parent-symbology definition pull that enumerates one day's SPXW chain."""

    return DayPull(
        dataset=DEFINITIONS_DATASET,
        schema=DEFINITIONS_SCHEMA,
        day=day,
        stype_in="parent",
        symbols=(SPXW_PARENT_SYMBOL,),
        scope=DEFINITIONS_SCOPE,
    )


def enumerate_expiring(definitions_csv: bytes, day: date) -> tuple[str, ...]:
    """Return the raw symbols whose expiration date equals ``day``, sorted.

    The definition CSV carries ``expiration`` as nanoseconds since the epoch at
    UTC midnight of the expiration date (column name verified live 2026-08-19);
    a ``maturity_date`` ISO-date column is accepted as a fallback. Raw symbols
    are 21-character padded OSI strings (root left-justified); the padding is
    part of the raw_symbol symbology and is preserved verbatim.
    """

    reader = csv.DictReader(io.StringIO(definitions_csv.decode("utf-8")))
    fieldnames = reader.fieldnames
    if not fieldnames:
        raise ValueError("definitions CSV has no header row")
    matches: set[str] = set()
    if "expiration" in fieldnames:
        for row in reader:
            raw = row.get("raw_symbol") or ""
            expiration = (row.get("expiration") or "").strip()
            if not raw or not expiration:
                continue
            nanos = int(expiration)
            if nanos == UNDEFINED_TIMESTAMP_NS:
                continue
            expiry = datetime.fromtimestamp(nanos // 1_000_000_000, tz=UTC).date()
            if expiry == day:
                matches.add(raw)
    elif "maturity_date" in fieldnames:
        for row in reader:
            raw = row.get("raw_symbol") or ""
            maturity = (row.get("maturity_date") or "").strip()
            if not raw or not maturity:
                continue
            if date.fromisoformat(maturity) == day:
                matches.add(raw)
    else:
        raise ValueError("definitions CSV has neither 'expiration' nor 'maturity_date'")
    return tuple(sorted(matches))


def _day_window_utc(day: date) -> tuple[str, str]:
    start_local = datetime.combine(day, time(0, 0), tzinfo=NEW_YORK)
    end_local = start_local + timedelta(days=1)
    start = start_local.astimezone(UTC).isoformat().replace("+00:00", "Z")
    end = end_local.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return start, end


class DatabentoHistoricalGateway:
    """Minimal historical-API client: cost estimate, record count, day download."""

    def __init__(self, settings: DatabentoSettings, session: Any | None = None) -> None:
        if not settings.has_credentials:
            raise ValueError("DATABENTO_API_KEY is required")
        self._settings = settings
        self._session = session if session is not None else requests.Session()
        key = settings.api_key
        assert key is not None  # narrowed by has_credentials
        self._auth = (key.get_secret_value(), "")

    def _params(self, pull: DayPull) -> dict[str, str]:
        start, end = _day_window_utc(pull.day)
        return {
            "dataset": pull.dataset,
            "schema": pull.schema,
            "start": start,
            "end": end,
            "symbols": ",".join(pull.symbols),
            "stype_in": pull.stype_in,
        }

    def _checked(self, response: Any) -> Any:
        # Databento streams large query results as 206 Partial Content.
        if response.status_code not in (200, 206):
            case = "unknown"
            try:
                detail = response.json().get("detail", {})
                if isinstance(detail, dict):
                    case = str(detail.get("case", "unknown"))
            except Exception:  # response body may not be JSON; keep the case generic
                case = "unparseable_error_body"
            raise DatabentoApiError(response.status_code, case)
        return response

    @staticmethod
    def _network_guard(call: Callable[[], Any]) -> Any:
        """Transport failures become a structured case; tracebacks never leak.

        Transient transport errors and retryable statuses (429/5xx) get two
        retries with short backoff; anything else surfaces once as a structured
        DatabentoApiError.
        """

        retryable_statuses = {0, 429, 500, 502, 503, 504}
        delays = (2.0, 5.0)
        for attempt in range(len(delays) + 1):
            if attempt:
                sleep(delays[attempt - 1])
            try:
                return call()
            except requests.exceptions.RequestException:
                if attempt == len(delays):
                    raise DatabentoApiError(0, "network_error") from None
            except DatabentoApiError as exc:
                if exc.status_code not in retryable_statuses or attempt == len(delays):
                    raise
        raise DatabentoApiError(0, "network_error")

    def estimate_cost(self, pull: DayPull) -> float:
        # POST with a form body: raw_symbol chains exceed the GET URL limit.
        response = self._network_guard(
            lambda: self._checked(
                self._session.post(
                    f"{self._settings.base_url}/metadata.get_cost",
                    auth=self._auth,
                    data=self._params(pull),
                    timeout=60,
                )
            )
        )
        return float(response.json())

    def record_count(self, pull: DayPull) -> int:
        response = self._network_guard(
            lambda: self._checked(
                self._session.post(
                    f"{self._settings.base_url}/metadata.get_record_count",
                    auth=self._auth,
                    data=self._params(pull),
                    timeout=60,
                )
            )
        )
        return int(response.json())

    def download_day(self, pull: DayPull) -> bytes:
        response = self._network_guard(
            lambda: self._checked(
                self._session.post(
                    f"{self._settings.base_url}/timeseries.get_range",
                    auth=self._auth,
                    data={**self._params(pull), "encoding": "dbn"},
                    timeout=(5, 300),
                )
            )
        )
        payload = bytes(response.content)
        if not payload:
            raise DatabentoApiError(200, "empty_payload")
        return payload

    def download_definitions(self, day: date) -> bytes:
        """Download one day's SPXW contract definitions as CSV (small schema)."""

        pull = spxw_definitions_pull(day)
        response = self._network_guard(
            lambda: self._checked(
                self._session.post(
                    f"{self._settings.base_url}/timeseries.get_range",
                    auth=self._auth,
                    data={**self._params(pull), "encoding": "csv"},
                    timeout=(5, 120),
                )
            )
        )
        payload = bytes(response.content)
        if not payload:
            raise DatabentoApiError(200, "empty_payload")
        return payload
