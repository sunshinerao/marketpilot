from __future__ import annotations

from datetime import date

import pytest

from marketpilot.adapters.databento import (
    DatabentoApiError,
    DatabentoHistoricalGateway,
    DatabentoSettings,
    DayPull,
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

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.last_params = kwargs.get("params")  # type: ignore[assignment]
        if url.endswith("metadata.get_cost"):
            return FakeResponse(200, json_value=self.cost_response)
        return FakeResponse(200, json_value=self.count_response)

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.last_params = kwargs.get("data")  # type: ignore[assignment]
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


def test_estimate_cost_uses_parent_symbology_and_et_day_window() -> None:
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
    session.get = lambda url, **kwargs: FakeResponse(  # type: ignore[method-assign]
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
