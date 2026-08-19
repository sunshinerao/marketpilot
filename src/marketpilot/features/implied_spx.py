"""Implied SPX coordinate and official anchor closes (Phase 5 workstream B).

The implied SPX series is an *approximation*:

    S_t ~= SPX_a x F_t / F_a

where ``SPX_a`` is the previous trading day's official SPX cash close and
``F_a`` is the previous trading day's last ES price (the 16:00 ET anchor).
Every consumer must label these values as *implied* SPX, never as official
SPX cash prints.

Official prior-day SPX cash closes come from free public sources only:
the Massive (formerly Polygon) aggregates endpoint for ``I:SPX`` when the
API key's plan entitles it, otherwise the free Cboe SPX EOD history CSV.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import UTC, date, datetime
from typing import Any

import requests

from marketpilot.features.day_structure import MinuteBar

MASSIVE_AGGS_URL = "https://api.massive.com/v2/aggs/ticker/I:SPX/range/1/day"
MASSIVE_API_KEY_ENV = "MASSIVE_API_KEY"
CBOE_SPX_CSV_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/SPX_History.csv"
DEFAULT_TIMEOUT_SECONDS = 30.0
CBOE_USER_AGENT = "MarketPilot calibration (research)"


class ImpliedSpxError(ValueError):
    """Raised when implied-SPX inputs violate the anchor-model contract."""


class AnchorCloseError(RuntimeError):
    """Raised when official SPX anchor closes cannot be loaded or parsed."""


def _require_positive(value: float, field_name: str) -> None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ImpliedSpxError(f"{field_name} must be a real number, got {type(value).__name__}")
    if value <= 0:
        raise ImpliedSpxError(f"{field_name} must be positive, got {value}")


def _require_ordered_bars(bars: tuple[MinuteBar, ...]) -> None:
    if not bars:
        raise ImpliedSpxError("bars must not be empty")
    if any(bars[i].ts > bars[i + 1].ts for i in range(len(bars) - 1)):
        raise ImpliedSpxError("bars must be time-ordered (non-decreasing ts)")


def implied_spx_series(
    bars: tuple[MinuteBar, ...],
    *,
    prior_cash_close: float,
    anchor_futures_price: float,
) -> tuple[tuple[datetime, float], ...]:
    """Return the implied SPX series ``(ts, value)`` for one session's ES bars.

    ``prior_cash_close`` is the previous trading day's official SPX cash close
    (``SPX_a``); ``anchor_futures_price`` is the previous trading day's last ES
    price (``F_a``, the 16:00 ET anchor). Each bar close ``F_t`` maps to
    ``SPX_a * F_t / F_a``. The result is an implied coordinate, not official
    SPX, and must be labelled as such downstream.
    """

    _require_positive(prior_cash_close, "prior_cash_close")
    _require_positive(anchor_futures_price, "anchor_futures_price")
    _require_ordered_bars(bars)
    ratio_denominator = anchor_futures_price
    series: list[tuple[datetime, float]] = []
    for bar in bars:
        _require_positive(bar.close, "bar.close")
        series.append((bar.ts, prior_cash_close * bar.close / ratio_denominator))
    return tuple(series)


def last_bar_close(bars: tuple[MinuteBar, ...]) -> float:
    """Return the last bar's close — the F_a anchor for the *next* session."""

    _require_ordered_bars(bars)
    _require_positive(bars[-1].close, "bars[-1].close")
    return bars[-1].close


def parse_massive_aggs(payload: dict[str, Any]) -> dict[date, float]:
    """Parse a Massive v2 aggregates payload into ``{date: close}``.

    Expects ``status == "OK"`` and ``results`` items with millisecond epoch
    ``t`` and close ``c``. Raises :class:`AnchorCloseError` on any deviation;
    a ``NOT_AUTHORIZED`` status raises a distinct message so callers can fall
    back to a free source.
    """

    status = payload.get("status")
    if status == "NOT_AUTHORIZED":
        raise AnchorCloseError("massive I:SPX aggregates not authorized on this plan")
    if status != "OK":
        raise AnchorCloseError(f"massive aggregates returned status={status!r}")
    results = payload.get("results")
    if not isinstance(results, list):
        raise AnchorCloseError("massive aggregates payload missing 'results' list")
    closes: dict[date, float] = {}
    for item in results:
        if not isinstance(item, dict) or "t" not in item or "c" not in item:
            raise AnchorCloseError("massive aggregates result missing 't'/'c' fields")
        day = datetime.fromtimestamp(item["t"] / 1000, tz=UTC).date()
        close = float(item["c"])
        if close <= 0:
            raise AnchorCloseError(f"massive aggregates non-positive close for {day}")
        closes[day] = close
    return closes


def parse_cboe_csv(text: str) -> dict[date, float]:
    """Parse the free Cboe SPX EOD history CSV (``DATE,SPX``) into ``{date: close}``."""

    closes: dict[date, float] = {}
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row or row[0].strip().upper() == "DATE":
            continue
        try:
            day = datetime.strptime(row[0].strip(), "%m/%d/%Y").date()
            close = float(row[1])
        except (ValueError, IndexError) as exc:
            raise AnchorCloseError(f"unparseable Cboe SPX CSV row: {row!r}") from exc
        if close <= 0:
            raise AnchorCloseError(f"Cboe SPX CSV non-positive close for {day}")
        closes[day] = close
    return closes


def _fetch_massive_closes(
    start: date,
    end: date,
    api_key: str,
    *,
    timeout: float,
    session: requests.Session | None,
) -> dict[date, float] | None:
    """Return closes from Massive, or ``None`` when the free tier cannot serve them."""

    http = session if session is not None else requests
    url = f"{MASSIVE_AGGS_URL}/{start.isoformat()}/{end.isoformat()}"
    response = http.get(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        params={"limit": 5000},
        timeout=timeout,
    )
    if response.status_code == 403:
        return None
    response.raise_for_status()
    try:
        return parse_massive_aggs(response.json())
    except AnchorCloseError:
        return None


def _fetch_cboe_closes(
    *,
    timeout: float,
    session: requests.Session | None,
) -> dict[date, float]:
    http = session if session is not None else requests
    response = http.get(
        CBOE_SPX_CSV_URL,
        headers={"User-Agent": CBOE_USER_AGENT},
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_cboe_csv(response.text)


def load_anchor_closes(
    start: date,
    end: date,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    session: requests.Session | None = None,
) -> dict[date, float]:
    """Load official SPX daily cash closes for ``[start, end]`` from free sources.

    Preferred source: Massive v2 aggregates for ``I:SPX`` (Authorization Bearer
    from the ``MASSIVE_API_KEY`` environment variable). When no key is set, the
    plan is not entitled to index aggregates (NOT_AUTHORIZED / 403), or the
    response is unusable, falls back to the free Cboe SPX EOD history CSV and
    filters it to the requested window.
    """

    if start > end:
        raise ImpliedSpxError(f"start {start} must not be after end {end}")
    api_key = os.environ.get(MASSIVE_API_KEY_ENV, "").strip()
    closes: dict[date, float] | None = None
    if api_key:
        closes = _fetch_massive_closes(start, end, api_key, timeout=timeout, session=session)
    if not closes:
        closes = _fetch_cboe_closes(timeout=timeout, session=session)
    windowed = {day: close for day, close in closes.items() if start <= day <= end}
    if not windowed:
        raise AnchorCloseError(f"no official SPX closes available in [{start}, {end}]")
    return windowed
