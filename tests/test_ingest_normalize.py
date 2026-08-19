"""Hermetic tests for ingest.normalize: frames are built in-memory, no DBN or I/O."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from marketpilot.features.day_structure import ChainDay, DayStructureError
from marketpilot.ingest import normalize
from marketpilot.ingest.normalize import (
    NormalizeError,
    cbbo_logical_key,
    chain_day_from_frames,
    es_logical_key,
    normalize_day,
)
from marketpilot.ingest.peek import LandedBatch

DAY = date(2026, 8, 17)
SYMBOL = "SPXW  260817C06400000"  # 21-character padded OSI
SYMBOL_B = "SPXW  260817P06400000"


def _ts(minute: int) -> pd.Timestamp:
    return pd.Timestamp(datetime(2026, 8, 17, 13, 30, tzinfo=UTC)) + pd.Timedelta(
        minutes=minute
    )


def _es_frame(
    minutes: tuple[int, ...] = (0, 1),
    *,
    as_index: bool = False,
) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "ts_event": [_ts(m) for m in minutes],
            "open": [6400.0 + m for m in minutes],
            "high": [6401.0 + m for m in minutes],
            "low": [6399.0 + m for m in minutes],
            "close": [6400.5 + m for m in minutes],
            "volume": [100.0 + m for m in minutes],
        }
    )
    if as_index:
        frame = frame.set_index("ts_event")
    return frame


def _cbbo_frame(
    minutes: tuple[int, ...] = (0, 1),
    *,
    bid: float | None = 10.0,
    ask: float | None = 10.5,
    symbol: str = SYMBOL,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_event": [_ts(m) for m in minutes],
            "symbol": [symbol for _ in minutes],
            "bid_px_00": [float("nan") if bid is None else bid for _ in minutes],
            "ask_px_00": [float("nan") if ask is None else ask for _ in minutes],
            "bid_sz_00": [5 for _ in minutes],
            "ask_sz_00": [7 for _ in minutes],
        }
    )


def test_logical_keys_follow_daypull_conventions() -> None:
    assert es_logical_key(DAY) == "GLBX.MDP3/ohlcv-1m/es-front-month/2026-08-17"
    assert cbbo_logical_key(DAY) == "OPRA.PILLAR/cbbo-1m/spxw-0dte/2026-08-17"


def test_chain_day_from_frames_happy_path() -> None:
    chain = chain_day_from_frames(DAY, _es_frame(), _cbbo_frame())

    assert isinstance(chain, ChainDay)
    assert chain.day == DAY
    assert len(chain.underlying_bars) == 2
    assert len(chain.quotes) == 2
    bar = chain.underlying_bars[0]
    assert bar.ts == datetime(2026, 8, 17, 13, 30, tzinfo=UTC)
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        6400.0,
        6401.0,
        6399.0,
        6400.5,
        100.0,
    )
    quote = chain.quotes[0]
    assert quote.symbol == SYMBOL
    assert (quote.bid, quote.ask, quote.bid_size, quote.ask_size) == (10.0, 10.5, 5, 7)


def test_ts_event_may_be_the_frame_index() -> None:
    # databento's DBNStore.to_df indexes by ts_event rather than keeping a column.
    chain = chain_day_from_frames(
        DAY, _es_frame(as_index=True), _cbbo_frame().set_index("ts_event")
    )
    assert len(chain.underlying_bars) == 2
    assert len(chain.quotes) == 2


def test_naive_timestamps_are_assumed_utc() -> None:
    es = _es_frame().assign(ts_event=lambda f: f["ts_event"].dt.tz_localize(None))
    cbbo = _cbbo_frame().assign(ts_event=lambda f: f["ts_event"].dt.tz_localize(None))

    chain = chain_day_from_frames(DAY, es, cbbo)

    assert chain.underlying_bars[0].ts == datetime(2026, 8, 17, 13, 30, tzinfo=UTC)
    assert chain.quotes[0].ts.tzinfo == UTC


def test_nan_bid_and_ask_become_none() -> None:
    chain = chain_day_from_frames(DAY, _es_frame(), _cbbo_frame(bid=None, ask=None))

    assert chain.quotes[0].bid is None
    assert chain.quotes[0].ask is None


def test_unordered_rows_are_sorted_by_timestamp() -> None:
    chain = chain_day_from_frames(DAY, _es_frame(minutes=(2, 0, 1)), _cbbo_frame((1, 0)))

    assert [bar.ts.minute for bar in chain.underlying_bars] == [30, 31, 32]
    assert [quote.ts.minute for quote in chain.quotes] == [30, 31]


def test_empty_frames_are_rejected() -> None:
    with pytest.raises(DayStructureError, match="underlying_bars must not be empty"):
        chain_day_from_frames(DAY, _es_frame(minutes=()), _cbbo_frame())
    with pytest.raises(DayStructureError, match="quotes must not be empty"):
        chain_day_from_frames(DAY, _es_frame(), _cbbo_frame(minutes=()))


def test_crossed_book_is_rejected() -> None:
    with pytest.raises(DayStructureError, match="bid must not exceed ask"):
        chain_day_from_frames(DAY, _es_frame(), _cbbo_frame(bid=11.0, ask=10.5))


def test_symbol_must_be_padded_osi() -> None:
    with pytest.raises(DayStructureError, match="21-character padded OSI"):
        chain_day_from_frames(DAY, _es_frame(), _cbbo_frame(symbol="SPXW2400C"))


def test_missing_columns_raise_normalize_error() -> None:
    with pytest.raises(NormalizeError, match="ohlcv-1m frame missing columns: volume"):
        chain_day_from_frames(DAY, _es_frame().drop(columns=["volume"]), _cbbo_frame())
    with pytest.raises(NormalizeError, match="cbbo-1m frame missing columns: ask_px_00"):
        chain_day_from_frames(DAY, _es_frame(), _cbbo_frame().drop(columns=["ask_px_00"]))


def test_frame_without_ts_event_raises_normalize_error() -> None:
    with pytest.raises(NormalizeError, match="no ts_event column or index"):
        chain_day_from_frames(
            DAY, _es_frame().drop(columns=["ts_event"]), _cbbo_frame()
        )


def test_normalize_day_missing_batch_raises_normalize_error(tmp_path: Path) -> None:
    with pytest.raises(NormalizeError, match="missing landed batch"):
        normalize_day(
            data_root=tmp_path / "raw",
            pit_ledger_path=tmp_path / "pit" / "records.jsonl",
            day=DAY,
        )


def test_normalize_day_reports_decode_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_load(*, data_root: object, pit_ledger_path: object, logical_key: str) -> LandedBatch:
        return LandedBatch(logical_key=logical_key, payload=b"not-dbn", schema="ohlcv-1m")

    monkeypatch.setattr(normalize, "load_landed_batch", fake_load)

    with pytest.raises(NormalizeError, match="failed to decode DBN batch"):
        normalize_day(
            data_root=tmp_path / "raw",
            pit_ledger_path=tmp_path / "pit" / "records.jsonl",
            day=DAY,
        )


def test_normalize_day_wires_batches_to_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = {
        es_logical_key(DAY): _es_frame(as_index=True),
        cbbo_logical_key(DAY): _cbbo_frame().set_index("ts_event"),
    }
    loaded_keys: list[str] = []

    def fake_load(*, data_root: object, pit_ledger_path: object, logical_key: str) -> LandedBatch:
        loaded_keys.append(logical_key)
        # The payload carries the key so the fake DBN decoder can route it.
        return LandedBatch(logical_key=logical_key, payload=logical_key.encode(), schema="x")

    class FakeStore:
        def __init__(self, frame: pd.DataFrame) -> None:
            self._frame = frame

        def to_df(self) -> pd.DataFrame:
            return self._frame

    import databento

    monkeypatch.setattr(normalize, "load_landed_batch", fake_load)
    monkeypatch.setattr(
        databento.DBNStore,
        "from_bytes",
        staticmethod(lambda payload: FakeStore(frames[payload.decode()])),
    )

    chain = normalize_day(
        data_root=tmp_path / "raw",
        pit_ledger_path=tmp_path / "pit" / "records.jsonl",
        day=DAY,
    )

    assert loaded_keys == [es_logical_key(DAY), cbbo_logical_key(DAY)]
    assert isinstance(chain, ChainDay)
    assert len(chain.underlying_bars) == 2
    assert len(chain.quotes) == 2
