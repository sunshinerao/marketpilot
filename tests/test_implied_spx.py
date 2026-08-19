"""Hermetic tests for features/implied_spx.py — no network, no data/raw."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from marketpilot.features.day_structure import MinuteBar
from marketpilot.features.implied_spx import (
    AnchorCloseError,
    ImpliedSpxError,
    implied_spx_series,
    last_bar_close,
    load_anchor_closes,
    parse_cboe_csv,
    parse_massive_aggs,
)

PRIOR_CASH_CLOSE = 5000.0
ANCHOR_FUTURES = 5050.0


def _bar(ts: datetime, close: float) -> MinuteBar:
    return MinuteBar(ts=ts, open=close, high=close, low=close, close=close, volume=100.0)


def _session(closes: list[float], start: datetime | None = None) -> tuple[MinuteBar, ...]:
    base = start or datetime(2026, 8, 19, 13, 30, tzinfo=UTC)
    return tuple(_bar(base + timedelta(minutes=i), close) for i, close in enumerate(closes))


class TestImpliedSpxSeries:
    def test_futures_up_one_percent_implies_spx_up_one_percent(self) -> None:
        bars = _session([ANCHOR_FUTURES, ANCHOR_FUTURES * 1.01])
        series = implied_spx_series(
            bars, prior_cash_close=PRIOR_CASH_CLOSE, anchor_futures_price=ANCHOR_FUTURES
        )
        assert len(series) == 2
        assert series[0][1] == pytest.approx(PRIOR_CASH_CLOSE)
        assert series[1][1] == pytest.approx(PRIOR_CASH_CLOSE * 1.01)

    def test_futures_down_moves_implied_down(self) -> None:
        bars = _session([ANCHOR_FUTURES * 0.98])
        series = implied_spx_series(
            bars, prior_cash_close=PRIOR_CASH_CLOSE, anchor_futures_price=ANCHOR_FUTURES
        )
        assert series[0][1] == pytest.approx(PRIOR_CASH_CLOSE * 0.98)

    def test_timestamps_are_carried_through(self) -> None:
        bars = _session([ANCHOR_FUTURES, ANCHOR_FUTURES * 1.005])
        series = implied_spx_series(
            bars, prior_cash_close=PRIOR_CASH_CLOSE, anchor_futures_price=ANCHOR_FUTURES
        )
        assert [ts for ts, _ in series] == [bar.ts for bar in bars]

    def test_empty_bars_rejected(self) -> None:
        with pytest.raises(ImpliedSpxError, match="empty"):
            implied_spx_series(
                (), prior_cash_close=PRIOR_CASH_CLOSE, anchor_futures_price=ANCHOR_FUTURES
            )

    def test_unordered_bars_rejected(self) -> None:
        base = datetime(2026, 8, 19, 13, 30, tzinfo=UTC)
        bars = (
            _bar(base + timedelta(minutes=1), ANCHOR_FUTURES),
            _bar(base, ANCHOR_FUTURES),
        )
        with pytest.raises(ImpliedSpxError, match="time-ordered"):
            implied_spx_series(
                bars, prior_cash_close=PRIOR_CASH_CLOSE, anchor_futures_price=ANCHOR_FUTURES
            )

    @pytest.mark.parametrize("prior_cash_close", [0.0, -5000.0])
    def test_non_positive_prior_cash_close_rejected(self, prior_cash_close: float) -> None:
        with pytest.raises(ImpliedSpxError, match="prior_cash_close"):
            implied_spx_series(
                _session([ANCHOR_FUTURES]),
                prior_cash_close=prior_cash_close,
                anchor_futures_price=ANCHOR_FUTURES,
            )

    @pytest.mark.parametrize("anchor", [0.0, -1.0])
    def test_non_positive_anchor_futures_rejected(self, anchor: float) -> None:
        with pytest.raises(ImpliedSpxError, match="anchor_futures_price"):
            implied_spx_series(
                _session([ANCHOR_FUTURES]),
                prior_cash_close=PRIOR_CASH_CLOSE,
                anchor_futures_price=anchor,
            )
    def test_non_numeric_prior_cash_close_rejected(self) -> None:
        with pytest.raises(ImpliedSpxError, match="real number"):
            implied_spx_series(
                _session([ANCHOR_FUTURES]),
                prior_cash_close="5000",  # type: ignore[arg-type]
                anchor_futures_price=ANCHOR_FUTURES,
            )


class TestLastBarClose:
    def test_returns_last_close_for_anchor_derivation(self) -> None:
        bars = _session([5040.0, 5042.5, 5051.25])
        assert last_bar_close(bars) == 5051.25

    def test_empty_bars_rejected(self) -> None:
        with pytest.raises(ImpliedSpxError, match="empty"):
            last_bar_close(())

    def test_anchor_round_trip(self) -> None:
        prior_day = _session([5040.0, 5051.25])
        anchor = last_bar_close(prior_day)
        today = _session([anchor * 1.002])
        series = implied_spx_series(
            today, prior_cash_close=PRIOR_CASH_CLOSE, anchor_futures_price=anchor
        )
        assert series[0][1] == pytest.approx(PRIOR_CASH_CLOSE * 1.002)


class TestParseMassiveAggs:
    def test_parses_canned_ok_payload(self) -> None:
        payload = {
            "ticker": "I:SPX",
            "status": "OK",
            "resultsCount": 2,
            "results": [
                {"t": 1786406400000, "c": 6445.76, "o": 6400.0, "h": 6450.0, "l": 6390.0},
                {"t": 1786492800000, "c": 6466.58, "o": 6446.0, "h": 6470.0, "l": 6440.0},
            ],
        }
        closes = parse_massive_aggs(payload)
        assert closes == {date(2026, 8, 11): 6445.76, date(2026, 8, 12): 6466.58}

    def test_not_authorized_raises(self) -> None:
        payload = {"status": "NOT_AUTHORIZED", "message": "You are not entitled to this data."}
        with pytest.raises(AnchorCloseError, match="not authorized"):
            parse_massive_aggs(payload)

    def test_error_status_raises(self) -> None:
        with pytest.raises(AnchorCloseError, match="status"):
            parse_massive_aggs({"status": "ERROR"})

    def test_missing_results_raises(self) -> None:
        with pytest.raises(AnchorCloseError, match="results"):
            parse_massive_aggs({"status": "OK"})

    def test_malformed_result_item_raises(self) -> None:
        with pytest.raises(AnchorCloseError, match="t.*c"):
            parse_massive_aggs({"status": "OK", "results": [{"t": 1786406400000}]})

    def test_non_positive_close_raises(self) -> None:
        payload = {"status": "OK", "results": [{"t": 1786406400000, "c": 0.0}]}
        with pytest.raises(AnchorCloseError, match="non-positive"):
            parse_massive_aggs(payload)


class TestParseCboeCsv:
    def test_parses_canned_csv(self) -> None:
        text = "DATE,SPX\n08/11/2026,6445.760000\n08/12/2026,6466.580000\n"
        assert parse_cboe_csv(text) == {date(2026, 8, 11): 6445.76, date(2026, 8, 12): 6466.58}

    def test_bad_row_raises(self) -> None:
        with pytest.raises(AnchorCloseError, match="unparseable"):
            parse_cboe_csv("DATE,SPX\nnot-a-date,123\n")

    def test_non_positive_close_raises(self) -> None:
        with pytest.raises(AnchorCloseError, match="non-positive"):
            parse_cboe_csv("DATE,SPX\n08/11/2026,-1\n")


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object] | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict[str, object] | None:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AnchorCloseError(f"HTTP {self.status_code}")


class _FakeSession:
    """Scripted stand-in for requests.Session; records URLs, never touches network."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = responses
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.urls.append(url)
        return self._responses.pop(0)


class TestLoadAnchorCloses:
    def test_start_after_end_rejected(self) -> None:
        with pytest.raises(ImpliedSpxError, match="must not be after"):
            load_anchor_closes(date(2026, 8, 12), date(2026, 8, 11))

    def test_massive_ok_payload_used_when_key_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        session = _FakeSession(
            [
                _FakeResponse(
                    200,
                    {
                        "status": "OK",
                        "results": [
                            {"t": 1786406400000, "c": 6445.76},
                            {"t": 1786492800000, "c": 6466.58},
                        ],
                    },
                )
            ]
        )
        closes = load_anchor_closes(
            date(2026, 8, 11), date(2026, 8, 12), session=session  # type: ignore[arg-type]
        )
        assert closes == {date(2026, 8, 11): 6445.76, date(2026, 8, 12): 6466.58}
        assert len(session.urls) == 1
        assert "api.massive.com" in session.urls[0]

    def test_massive_403_falls_back_to_cboe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        session = _FakeSession(
            [
                _FakeResponse(403, {"status": "NOT_AUTHORIZED"}),
                _FakeResponse(
                    200,
                    text="DATE,SPX\n08/10/2026,6400.0\n08/11/2026,6445.76\n08/12/2026,6466.58\n",
                ),
            ]
        )
        closes = load_anchor_closes(
            date(2026, 8, 11), date(2026, 8, 12), session=session  # type: ignore[arg-type]
        )
        assert closes == {date(2026, 8, 11): 6445.76, date(2026, 8, 12): 6466.58}
        assert "cdn.cboe.com" in session.urls[1]

    def test_massive_not_authorizized_body_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
        session = _FakeSession(
            [
                _FakeResponse(200, {"status": "NOT_AUTHORIZED"}),
                _FakeResponse(200, text="DATE,SPX\n08/11/2026,6445.76\n"),
            ]
        )
        closes = load_anchor_closes(
            date(2026, 8, 11), date(2026, 8, 11), session=session  # type: ignore[arg-type]
        )
        assert closes == {date(2026, 8, 11): 6445.76}

    def test_no_api_key_goes_straight_to_cboe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
        session = _FakeSession([_FakeResponse(200, text="DATE,SPX\n08/11/2026,6445.76\n")])
        closes = load_anchor_closes(
            date(2026, 8, 11), date(2026, 8, 11), session=session  # type: ignore[arg-type]
        )
        assert closes == {date(2026, 8, 11): 6445.76}
        assert len(session.urls) == 1
        assert "cdn.cboe.com" in session.urls[0]

    def test_cboe_window_filtering_excludes_out_of_range(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
        session = _FakeSession(
            [_FakeResponse(200, text="DATE,SPX\n01/02/1975,70.23\n08/11/2026,6445.76\n")]
        )
        closes = load_anchor_closes(
            date(2026, 8, 11), date(2026, 8, 11), session=session  # type: ignore[arg-type]
        )
        assert closes == {date(2026, 8, 11): 6445.76}

    def test_empty_window_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
        session = _FakeSession([_FakeResponse(200, text="DATE,SPX\n01/02/1975,70.23\n")])
        with pytest.raises(AnchorCloseError, match="no official SPX closes"):
            load_anchor_closes(
                date(2026, 8, 11), date(2026, 8, 12), session=session  # type: ignore[arg-type]
            )
