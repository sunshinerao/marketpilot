"""Workstream A (normalize): landed DBN batches → typed ``ChainDay`` structures.

One trading day of calibration input is two landed batches resolved through the
PIT ledger: ES front-month minute bars (``GLBX.MDP3/ohlcv-1m/es-front-month``)
and the SPXW 0DTE minute NBBO chain (``OPRA.PILLAR/cbbo-1m/spxw-0dte``). The
DataFrame→domain conversion is a pure function (``chain_day_from_frames``) so
it is testable without touching the encrypted landing boundary.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from marketpilot.features.day_structure import ChainDay, MinuteBar, OptionQuote
from marketpilot.ingest.peek import LandedBatch, PeekError, load_landed_batch

ES_DATASET = "GLBX.MDP3"
ES_SCHEMA = "ohlcv-1m"
ES_SCOPE = "es-front-month"
CBBO_DATASET = "OPRA.PILLAR"
CBBO_SCHEMA = "cbbo-1m"
CBBO_SCOPE = "spxw-0dte"

_ES_PRICE_COLUMNS = ("open", "high", "low", "close", "volume")
_CBBO_COLUMNS = ("symbol", "bid_px_00", "ask_px_00", "bid_sz_00", "ask_sz_00")


class NormalizeError(ValueError):
    """Raised when a day cannot be normalized from its landed batches."""


def es_logical_key(day: date) -> str:
    """PIT-ledger key for the day's ES front-month minute bars."""

    return f"{ES_DATASET}/{ES_SCHEMA}/{ES_SCOPE}/{day.isoformat()}"


def cbbo_logical_key(day: date) -> str:
    """PIT-ledger key for the day's SPXW 0DTE minute NBBO chain."""

    return f"{CBBO_DATASET}/{CBBO_SCHEMA}/{CBBO_SCOPE}/{day.isoformat()}"


def _require_columns(frame: pd.DataFrame, required: tuple[str, ...], label: str) -> None:
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise NormalizeError(f"{label} frame missing columns: {', '.join(missing)}")


def _ts_values(frame: pd.DataFrame, label: str) -> Any:
    """The ``ts_event`` series, whether carried as a column or as the index.

    ``databento.DBNStore.to_df`` indexes by ``ts_event``; hand-built frames in
    tests and future CSV decoders keep it as a column. Both are accepted.
    """

    if "ts_event" in frame.columns:
        return frame["ts_event"]
    if frame.index.name == "ts_event":
        return frame.index.to_series()
    raise NormalizeError(f"{label} frame has no ts_event column or index")


def _utc(value: Any) -> datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    result: datetime = ts.tz_convert("UTC").to_pydatetime()
    return result


def _minute_bars(es_frame: pd.DataFrame) -> tuple[MinuteBar, ...]:
    _require_columns(es_frame, _ES_PRICE_COLUMNS, "ohlcv-1m")
    ts_values = _ts_values(es_frame, "ohlcv-1m")
    bars = [
        MinuteBar(
            ts=_utc(ts),
            open=float(open_),
            high=float(high),
            low=float(low),
            close=float(close),
            volume=float(volume),
        )
        for ts, open_, high, low, close, volume in zip(
            ts_values,
            es_frame["open"],
            es_frame["high"],
            es_frame["low"],
            es_frame["close"],
            es_frame["volume"],
            strict=True,
        )
    ]
    # Structural violations (bad OHLC envelope, negative volume) propagate as
    # DayStructureError from the frozen contract; sorting enforces time order.
    bars.sort(key=lambda bar: bar.ts)
    return tuple(bars)


def _option_quotes(cbbo_frame: pd.DataFrame) -> tuple[OptionQuote, ...]:
    _require_columns(cbbo_frame, _CBBO_COLUMNS, "cbbo-1m")
    ts_values = _ts_values(cbbo_frame, "cbbo-1m")
    quotes = [
        OptionQuote(
            ts=_utc(ts),
            symbol=str(symbol),
            bid=None if pd.isna(bid) else float(bid),
            ask=None if pd.isna(ask) else float(ask),
            bid_size=int(bid_size),
            ask_size=int(ask_size),
        )
        for ts, symbol, bid, ask, bid_size, ask_size in zip(
            ts_values,
            cbbo_frame["symbol"],
            cbbo_frame["bid_px_00"],
            cbbo_frame["ask_px_00"],
            cbbo_frame["bid_sz_00"],
            cbbo_frame["ask_sz_00"],
            strict=True,
        )
    ]
    # Crossed books, negative prices/sizes, and unpadded symbols propagate as
    # DayStructureError from the frozen contract; NaN bid/ask became None.
    quotes.sort(key=lambda quote: (quote.ts, quote.symbol))
    return tuple(quotes)


def chain_day_from_frames(
    day: date,
    es_frame: pd.DataFrame,
    cbbo_frame: pd.DataFrame,
) -> ChainDay:
    """Pure conversion: ES ohlcv-1m frame + SPXW cbbo-1m frame → ``ChainDay``.

    Rows are sorted by timestamp (the contract requires time-ordered tuples);
    NaN bid/ask prices become ``None``. Structural violations — empty frames,
    crossed books, unpadded symbols, broken OHLC envelopes — raise
    ``DayStructureError`` from the frozen domain contract; schema violations
    (missing columns, no ``ts_event``) raise ``NormalizeError``.
    """

    return ChainDay(
        day=day,
        underlying_bars=_minute_bars(es_frame),
        quotes=_option_quotes(cbbo_frame),
    )


def _dbn_to_frame(batch: LandedBatch) -> pd.DataFrame:
    from databento import DBNStore  # imported lazily to keep startup light

    try:
        return DBNStore.from_bytes(batch.payload).to_df()
    except Exception as exc:
        raise NormalizeError(
            f"failed to decode DBN batch {batch.logical_key}: {exc}"
        ) from exc


def _load_decoded(*, data_root: str | Path, pit_ledger_path: str | Path, key: str) -> pd.DataFrame:
    try:
        batch = load_landed_batch(
            data_root=data_root,
            pit_ledger_path=pit_ledger_path,
            logical_key=key,
        )
    except PeekError as exc:
        raise NormalizeError(f"missing landed batch: {exc}") from exc
    return _dbn_to_frame(batch)


def normalize_day(
    *,
    data_root: str | Path,
    pit_ledger_path: str | Path,
    day: date,
) -> ChainDay:
    """Load and normalize one trading day from its two landed DBN batches."""

    es_frame = _load_decoded(
        data_root=data_root,
        pit_ledger_path=pit_ledger_path,
        key=es_logical_key(day),
    )
    cbbo_frame = _load_decoded(
        data_root=data_root,
        pit_ledger_path=pit_ledger_path,
        key=cbbo_logical_key(day),
    )
    return chain_day_from_frames(day, es_frame, cbbo_frame)
