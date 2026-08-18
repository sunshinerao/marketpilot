from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from marketpilot.adapters.databento import (
    UNDEFINED_TIMESTAMP_NS,
    DatabentoApiError,
    DatabentoHistoricalGateway,
    DatabentoSettings,
    DayPull,
    enumerate_expiring,
    spxw_definitions_pull,
)


class FakeResponse:
    def __init__(self, status_code: int, json_value: object = None, content: bytes = b""):
        self.status_code = status_code
        self._json_value = json_value
        self.content = content

    def json(self) -> object:
        if self._json_value is None:
            raise ValueError("not json")
        return self._json_value


class FakeSession:
    def __init__(self) -> None:
        self.cost_response: object = 1.25
        self.count_response: object = 42
        self.download = FakeResponse(200, content=b"dbn-bytes")
        self.last_params: dict[str, str] | None = None

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.last_params = kwargs.get("data")  # type: ignore[assignment]
        if url.endswith("metadata.get_cost"):
            return FakeResponse(200, json_value=self.cost_response)
        if url.endswith("metadata.get_record_count"):
            return FakeResponse(200, json_value=self.count_response)
        return self.download


def pull() -> DayPull:
    return DayPull(
        dataset="OPRA.PILLAR",
        schema="cbbo-1m",
        day=date(2026, 8, 17),
        stype_in="parent",
        symbols=("SPXW.OPT",),
        scope="spxw-whole-chain",
    )


def gateway(session: FakeSession) -> DatabentoHistoricalGateway:
    return DatabentoHistoricalGateway(
        DatabentoSettings(api_key="test-key"),
        session=session,
    )


def test_gateway_requires_credentials() -> None:
    with pytest.raises(ValueError, match="DATABENTO_API_KEY"):
        DatabentoHistoricalGateway(DatabentoSettings(api_key=None))


def test_estimate_cost_posts_parent_symbology_and_et_day_window() -> None:
    session = FakeSession()
    quote = gateway(session).estimate_cost(pull())

    assert quote == 1.25
    assert session.last_params is not None
    assert session.last_params["stype_in"] == "parent"
    assert session.last_params["symbols"] == "SPXW.OPT"
    # 2026-08-17 is EDT (UTC-4): ET midnight becomes 04:00Z.
    assert session.last_params["start"] == "2026-08-17T04:00:00+00:00".replace("+00:00", "Z")
    assert session.last_params["end"] == "2026-08-18T04:00:00Z"


def test_download_day_returns_bytes_and_empty_payload_is_an_error() -> None:
    session = FakeSession()
    assert gateway(session).download_day(pull()) == b"dbn-bytes"

    empty = FakeSession()
    empty.download = FakeResponse(200, content=b"")
    with pytest.raises(DatabentoApiError, match="empty_payload"):
        gateway(empty).download_day(pull())


def test_streamed_large_response_with_206_is_accepted() -> None:
    session = FakeSession()
    session.download = FakeResponse(206, content=b"partial-content-stream")

    assert gateway(session).download_day(pull()) == b"partial-content-stream"


def test_api_error_is_reduced_to_status_and_case() -> None:
    session = FakeSession()
    session.cost_response = None
    session.post = lambda url, **kwargs: FakeResponse(  # type: ignore[method-assign]
        400, json_value={"detail": {"case": "symbology_invalid_symbol"}}
    )

    with pytest.raises(DatabentoApiError) as excinfo:
        gateway(session).estimate_cost(pull())

    assert excinfo.value.status_code == 400
    assert excinfo.value.case == "symbology_invalid_symbol"
    assert "test-key" not in str(excinfo.value)


def test_day_pull_validation() -> None:
    with pytest.raises(ValueError, match="stype_in"):
        DayPull(
            dataset="OPRA.PILLAR",
            schema="cbbo-1m",
            day=date(2026, 8, 17),
            stype_in="yesterday",
            symbols=("SPXW.OPT",),
            scope="scope",
        )
    assert pull().logical_key == "OPRA.PILLAR/cbbo-1m/spxw-whole-chain/2026-08-17"


def _ns(day: date) -> int:
    """Nanoseconds since the epoch at UTC midnight, the definition expiration encoding."""

    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1_000_000_000


def _osi(day: date, right: str, strike: str) -> str:
    """A 21-character padded OSI raw symbol: root left-justified in 6 chars."""

    return f"{'SPXW'.ljust(6)}{day:%y%m%d}{right}{strike}"


def _definitions_csv(rows: list[tuple[str, str]], expiration_column: str = "expiration") -> bytes:
    lines = [f"raw_symbol,{expiration_column}"] + [f"{symbol},{value}" for symbol, value in rows]
    return ("\n".join(lines) + "\n").encode()


def test_download_definitions_uses_csv_encoding_and_definition_schema() -> None:
    session = FakeSession()
    session.download = FakeResponse(200, content=b"raw_symbol,expiration\n")

    payload = gateway(session).download_definitions(date(2026, 8, 14))

    assert payload == b"raw_symbol,expiration\n"
    assert session.last_params is not None
    assert session.last_params["encoding"] == "csv"
    assert session.last_params["schema"] == "definition"
    assert session.last_params["dataset"] == "OPRA.PILLAR"
    assert session.last_params["stype_in"] == "parent"
    assert session.last_params["symbols"] == "SPXW.OPT"

    empty = FakeSession()
    empty.download = FakeResponse(200, content=b"")
    with pytest.raises(DatabentoApiError, match="empty_payload"):
        gateway(empty).download_definitions(date(2026, 8, 14))


def test_spxw_definitions_pull_identity() -> None:
    pull = spxw_definitions_pull(date(2026, 8, 14))
    assert pull.logical_key == "OPRA.PILLAR/definition/spxw-definitions/2026-08-14"
    assert pull.stype_in == "parent"


def test_enumerate_expiring_keeps_padded_symbols_and_matches_only_the_day() -> None:
    day = date(2026, 8, 17)
    call = _osi(day, "C", "06000000")
    put = _osi(day, "P", "06000000")
    assert len(call) == 21
    csv_bytes = _definitions_csv(
        [
            (put, str(_ns(day))),
            (call, str(_ns(day))),
            (call, str(_ns(day))),  # repeated definition row dedupes
            (_osi(day + timedelta(days=7), "C", "06000000"), str(_ns(day + timedelta(days=7)))),
            (_osi(day, "C", "06050000"), str(UNDEFINED_TIMESTAMP_NS)),
        ]
    )

    symbols = enumerate_expiring(csv_bytes, day)

    assert symbols == tuple(sorted({call, put}))
    assert all(len(symbol) == 21 for symbol in symbols)
    assert symbols[0].startswith("SPXW  ")  # exact padding preserved


def test_enumerate_expiring_returns_empty_when_nothing_expires_that_day() -> None:
    day = date(2026, 8, 17)
    csv_bytes = _definitions_csv(
        [(_osi(day + timedelta(days=7), "P", "06000000"), str(_ns(day + timedelta(days=7))))]
    )

    assert enumerate_expiring(csv_bytes, day) == ()


def test_enumerate_expiring_accepts_maturity_date_fallback_column() -> None:
    day = date(2026, 8, 17)
    symbol = _osi(day, "C", "06000000")
    csv_bytes = _definitions_csv(
        [(symbol, day.isoformat()), (_osi(day, "P", "06000000"), "2026-08-24")],
        expiration_column="maturity_date",
    )

    assert enumerate_expiring(csv_bytes, day) == (symbol,)


def test_enumerate_expiring_rejects_csv_without_expiration_columns() -> None:
    with pytest.raises(ValueError, match="expiration"):
        enumerate_expiring(b"raw_symbol,foo\nSPXW,1\n", date(2026, 8, 17))
    with pytest.raises(ValueError, match="header"):
        enumerate_expiring(b"", date(2026, 8, 17))
