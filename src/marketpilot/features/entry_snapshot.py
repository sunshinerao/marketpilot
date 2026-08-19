"""Workstream E: entry-time chain feature extraction.

Pure feature computation over a normalized :class:`ChainDay` at the candidate
entry instant (default 09:45 ET), producing the frozen :class:`EntryFeatures`
contract. The implied SPX level (``implied_center``, from workstream B/C
labels) doubles as the Black-Scholes spot — 0DTE options on an implied index
level, an explicitly labelled approximation.

Honesty rules:

- ``atm_iv`` is recovered by Black-Scholes inversion of real NBBO mids, never
  fabricated. Quotes that are one-sided, penny (mid < 0.10), or absurdly wide
  (spread > 50% of mid) are excluded; when no contract qualifies or every
  inversion falls outside the bisection bracket, ``atm_iv_valid=False`` and
  ``atm_iv``/``skew`` are emitted as 0.0 with the record still auditable.
- ``realized_vol_30m`` is 0.0 when fewer than 15 underlying bars exist in the
  30 minutes before entry; ``median_spread`` is 0.0 when no two-sided quote
  sits within ±3% of center. 0.0 means "no honest value", not a measurement.

Black-Scholes inversion guards:

- Time to expiry in years is ``(expiry_close - entry) / 365.25 days``; a
  non-positive tau raises :class:`EntryFeaturesError` (caller configuration
  bug, not data).
- Bisection brackets sigma on [1e-4, 5.0] with 64 iterations. BS price is
  strictly monotone in sigma, so a mid inside the endpoint prices converges
  deterministically; a mid outside them (below the near-zero-sigma intrinsic
  floor or above the 500% vol ceiling) is a stale/crossed-print artifact and
  the contract is dropped, not clamped.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from marketpilot.features.day_structure import ChainDay, MinuteBar, OptionQuote
from marketpilot.features.entry_features import EntryFeatures, EntryFeaturesError

_SECONDS_PER_YEAR = 365.25 * 86_400

_IV_BRACKET_LO = 1e-4
_IV_BRACKET_HI = 5.0
_IV_BISECTION_ITERATIONS = 64

#: Minimum NBBO mid for a contract to enter IV inversion (drops penny noise).
_MIN_MID = 0.10
#: Maximum (ask - bid) as a fraction of mid for a quote to be considered sane.
_MAX_RELATIVE_SPREAD = 0.5
#: Number of nearest-to-center strikes entering the ATM IV median.
_ATM_CANDIDATES = 4
#: Skew legs sit at the strikes nearest ±2% of center.
_SKEW_OFFSET = 0.02
#: Median-spread band: strikes within ±3% of center.
_SPREAD_BAND = 0.03

_RV_WINDOW = timedelta(minutes=30)
_RV_MIN_BARS = 15
#: Annualization for RTH minute-bar log returns: sqrt(252 trading days * 390).
_RV_ANNUALIZATION = math.sqrt(252 * 390)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_price(
    *,
    spot: float,
    strike: float,
    rate: float,
    sigma: float,
    tau: float,
    is_call: bool,
) -> float:
    """Plain Black-Scholes price (no dividends; ``rate`` discounts the strike).

    Degenerate limits: ``tau <= 0`` collapses to intrinsic; ``sigma <= 0``
    collapses to the discounted forward intrinsic.
    """

    if spot <= 0 or strike <= 0:
        raise EntryFeaturesError("spot and strike must be positive")
    if tau <= 0.0:
        return max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    discount = math.exp(-rate * tau)
    if sigma <= 0.0:
        return (
            max(spot - strike * discount, 0.0)
            if is_call
            else max(strike * discount - spot, 0.0)
        )
    vol_sqrt_t = sigma * math.sqrt(tau)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * tau) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    if is_call:
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(
    *,
    price: float,
    spot: float,
    strike: float,
    rate: float,
    tau: float,
    is_call: bool,
) -> float | None:
    """Invert Black-Scholes for sigma; ``None`` when the price is unbracketed.

    Bisection on [1e-4, 5.0], 64 iterations. A price below the low-sigma
    endpoint (under the intrinsic floor) or above the high-sigma endpoint
    cannot come from any sane vol and is reported as a failed inversion.
    """

    if price < 0 or tau <= 0:
        return None
    lo = _IV_BRACKET_LO
    hi = _IV_BRACKET_HI
    p_lo = black_scholes_price(
        spot=spot, strike=strike, rate=rate, sigma=lo, tau=tau, is_call=is_call
    )
    p_hi = black_scholes_price(
        spot=spot, strike=strike, rate=rate, sigma=hi, tau=tau, is_call=is_call
    )
    tolerance = 1e-9 * max(1.0, price)
    if price < p_lo - tolerance or price > p_hi + tolerance:
        return None
    for _ in range(_IV_BISECTION_ITERATIONS):
        mid_sigma = 0.5 * (lo + hi)
        p_mid = black_scholes_price(
            spot=spot, strike=strike, rate=rate, sigma=mid_sigma, tau=tau, is_call=is_call
        )
        if p_mid < price:
            lo = mid_sigma
        else:
            hi = mid_sigma
    return 0.5 * (lo + hi)


def parse_osi(symbol: str) -> tuple[bool, float]:
    """Parse a 21-character padded OSI symbol → (is_call, strike).

    Layout: 6-char left-justified root, YYMMDD expiry, C/P flag, 8-digit
    strike scaled by 1000. The chain is 0DTE so the expiry field is not
    needed here; malformed symbols raise :class:`EntryFeaturesError` (a
    structural violation at the normalize boundary, not a feature gap).
    """

    if len(symbol) != 21:
        raise EntryFeaturesError(f"OSI symbol must be 21 characters, got {symbol!r}")
    expiry_raw = symbol[6:12]
    cp_flag = symbol[12]
    strike_raw = symbol[13:21]
    if not expiry_raw.isdigit() or cp_flag not in ("C", "P") or not strike_raw.isdigit():
        raise EntryFeaturesError(f"malformed OSI symbol {symbol!r}")
    return cp_flag == "C", int(strike_raw) / 1000.0


@dataclass(frozen=True, slots=True)
class _EntryQuote:
    """One parsed, two-sided quote selected at the entry instant."""

    strike: float
    is_call: bool
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid


def _quotes_at_entry(chain: ChainDay, entry: datetime) -> dict[str, OptionQuote]:
    """Latest quote at-or-before ``entry`` per symbol (chain is time-ordered)."""

    latest: dict[str, OptionQuote] = {}
    for quote in chain.quotes:
        if quote.ts > entry:
            break
        latest[quote.symbol] = quote
    return latest


def _two_sided(quotes: dict[str, OptionQuote]) -> list[_EntryQuote]:
    parsed: list[_EntryQuote] = []
    for quote in quotes.values():
        if quote.bid is None or quote.ask is None:
            continue
        is_call, strike = parse_osi(quote.symbol)
        parsed.append(_EntryQuote(strike=strike, is_call=is_call, bid=quote.bid, ask=quote.ask))
    return parsed


def _sane(quote: _EntryQuote) -> bool:
    """Quote-quality gate for IV inversion: real mid, bounded relative spread."""

    return quote.mid >= _MIN_MID and quote.spread <= _MAX_RELATIVE_SPREAD * quote.mid


def _atm_iv(candidates: list[_EntryQuote], center: float, tau: float, rate: float) -> float | None:
    """Median inverted IV over the 2-4 strikes nearest center.

    Calls above (or at) center, puts below — each contract is inverted on its
    own side. Contracts whose inversion fails to bracket are dropped; ``None``
    when no contract both qualifies and converges.
    """

    atm_side = [q for q in candidates if q.is_call == (q.strike >= center)]
    nearest = sorted(atm_side, key=lambda q: (abs(q.strike - center), q.strike))[:_ATM_CANDIDATES]
    ivs = [
        iv
        for q in nearest
        if (
            iv := implied_vol(
                price=q.mid, spot=center, strike=q.strike, rate=rate, tau=tau, is_call=q.is_call
            )
        )
        is not None
    ]
    if not ivs:
        return None
    return statistics.median(ivs)


def _skew_leg(
    candidates: list[_EntryQuote],
    target: float,
    *,
    is_call: bool,
    center: float,
    tau: float,
    rate: float,
) -> float | None:
    """Inverted IV of the contract nearest ``target`` on one side, if it converges."""

    side = [q for q in candidates if q.is_call is is_call]
    if not side:
        return None
    nearest = min(side, key=lambda q: (abs(q.strike - target), q.strike))
    return implied_vol(
        price=nearest.mid, spot=center, strike=nearest.strike, rate=rate, tau=tau, is_call=is_call
    )


def _realized_vol_30m(bars: tuple[MinuteBar, ...], entry: datetime) -> float:
    """Annualized stdev of close-to-close log returns in [entry-30m, entry).

    Needs at least 15 positive-close bars in the window; otherwise 0.0
    ("no honest value"). Annualized by sqrt(252 * 390) for RTH minutes.
    """

    window_start = entry - _RV_WINDOW
    closes = [bar.close for bar in bars if window_start <= bar.ts < entry and bar.close > 0]
    if len(closes) < _RV_MIN_BARS:
        return 0.0
    returns = [math.log(closes[i + 1] / closes[i]) for i in range(len(closes) - 1)]
    if len(returns) < 2:
        return 0.0
    return statistics.stdev(returns) * _RV_ANNUALIZATION


def _median_spread(quotes: list[_EntryQuote], center: float) -> float:
    """Median (ask - bid) over two-sided quotes within ±3% of center; 0.0 if none."""

    band_lo = center * (1.0 - _SPREAD_BAND)
    band_hi = center * (1.0 + _SPREAD_BAND)
    spreads = [q.spread for q in quotes if band_lo <= q.strike <= band_hi]
    if not spreads:
        return 0.0
    return statistics.median(spreads)


def compute_entry_features(
    *,
    chain: ChainDay,
    entry: datetime,
    implied_center: float,
    expiry_close: datetime,
    risk_free: float = 0.045,
) -> EntryFeatures:
    """Compute the entry-time feature snapshot for one normalized day.

    ``implied_center`` is the implied SPX level at entry (label ``entry_price``)
    and serves as the Black-Scholes spot. ``expiry_close`` is the 0DTE session
    close that bounds time to expiry. Never raises for data-quality problems —
    unrecoverable IV legs surface as ``atm_iv_valid=False`` with zeroed values.
    """

    tau = (expiry_close - entry).total_seconds() / _SECONDS_PER_YEAR
    if tau <= 0:
        raise EntryFeaturesError(
            f"expiry_close {expiry_close.isoformat()} must be after entry {entry.isoformat()}"
        )
    two_sided = _two_sided(_quotes_at_entry(chain, entry))
    candidates = [q for q in two_sided if _sane(q)]

    atm_iv_value = _atm_iv(candidates, implied_center, tau, risk_free)
    atm_iv_valid = atm_iv_value is not None

    skew = 0.0
    put_iv = _skew_leg(
        candidates,
        implied_center * (1.0 - _SKEW_OFFSET),
        is_call=False,
        center=implied_center,
        tau=tau,
        rate=risk_free,
    )
    call_iv = _skew_leg(
        candidates,
        implied_center * (1.0 + _SKEW_OFFSET),
        is_call=True,
        center=implied_center,
        tau=tau,
        rate=risk_free,
    )
    if put_iv is not None and call_iv is not None:
        skew = put_iv - call_iv

    return EntryFeatures(
        day=chain.day,
        entry_ts=entry,
        implied_center=implied_center,
        atm_iv=atm_iv_value if atm_iv_value is not None else 0.0,
        skew=skew,
        realized_vol_30m=_realized_vol_30m(chain.underlying_bars, entry),
        median_spread=_median_spread(two_sided, implied_center),
        atm_iv_valid=atm_iv_valid,
    )
