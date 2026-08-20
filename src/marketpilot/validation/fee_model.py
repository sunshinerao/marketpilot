"""Fee-aware economics: versioned fee schedule and condor fee arithmetic.

The v1 economics (see ``docs/development/calibration-report-v1.md``) priced
0DTE iron condors on conservative NBBO fills but charged no commissions, so
EV was slightly overestimated. This module closes that gap with a versioned,
validated fee schedule (``config/fees-v1.toml``):

- :class:`FeeSchedule` is the frozen, fail-closed contract: every fee is
  finite and non-negative, and the file must declare ``version = "fees-v1"``
  exactly — an unrecognized version is rejected rather than silently
  interpreted.
- :func:`fees_for_condor` prices the four legs of one iron condor. The
  default is opening-side fees only, because 0DTE positions expire without
  closing trades; ``round_trip=True`` doubles the total for strategies that
  do close.
- :func:`fee_adjusted_pnl` subtracts fees from a gross PnL. Units are the
  caller's responsibility: condor day PnL is carried in index points, so
  USD fees must be divided by the point value (SPXW pays 100 USD/point, see
  :data:`SPXW_POINT_VALUE_USD`) before adjustment.

Values in the shipped config are conservative estimates pending broker
statements; SPXW commission-free offers are deliberately NOT assumed.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Only fee-schedule version this loader accepts (fail-closed versioning).
EXPECTED_VERSION = "fees-v1"

#: An iron condor always has exactly four legs.
CONDOR_LEG_COUNT = 4

#: USD per index point for one SPXW contract (the Cboe multiplier).
SPXW_POINT_VALUE_USD = 100.0

DEFAULT_FEES_PATH = Path("config/fees-v1.toml")


class FeeModelError(ValueError):
    """Raised when a fee schedule or fee input violates its contract."""


def _finite_non_negative(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise FeeModelError(f"{name} must be finite")
    if result < 0:
        raise FeeModelError(f"{name} must not be negative")
    return result


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """A validated, versioned fee schedule (all monetary values in USD).

    ``commission_per_contract_usd`` is the broker commission per contract per
    side; ``regulatory_per_contract_usd`` bundles exchange/OCF/ORF per
    contract per side; ``fixed_per_order_usd`` is charged once per order.
    """

    version: str
    commission_per_contract_usd: float
    regulatory_per_contract_usd: float
    fixed_per_order_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.version != EXPECTED_VERSION:
            raise FeeModelError(
                f"fee schedule version must be {EXPECTED_VERSION!r}, got {self.version!r}"
            )
        for name in (
            "commission_per_contract_usd",
            "regulatory_per_contract_usd",
            "fixed_per_order_usd",
        ):
            object.__setattr__(self, name, _finite_non_negative(getattr(self, name), name))


def _required_number(mapping: Any, key: str, table: str) -> float:
    if not isinstance(mapping, dict) or key not in mapping:
        raise FeeModelError(f"fee schedule is missing [{table}] {key}")
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FeeModelError(f"fee schedule [{table}] {key} must be numeric")
    return float(value)


def load_fee_schedule(path: str | Path) -> FeeSchedule:
    """Load and validate a versioned fee-schedule TOML file.

    Fail-closed: a missing file, malformed TOML, wrong ``version``, missing
    keys, non-numeric or negative values all raise :class:`FeeModelError`
    (or the underlying ``OSError``/``tomllib.TOMLDecodeError``).
    """

    source = Path(path)
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise FeeModelError(f"malformed fee schedule TOML in {source}: {exc}") from exc
    version = raw.get("version")
    if not isinstance(version, str):
        raise FeeModelError(f"fee schedule in {source} must declare a string version")
    return FeeSchedule(
        version=version,
        commission_per_contract_usd=_required_number(
            raw.get("per_contract"), "commission_usd", "per_contract"
        ),
        regulatory_per_contract_usd=_required_number(
            raw.get("per_contract"), "regulatory_usd", "per_contract"
        ),
        fixed_per_order_usd=_required_number(raw.get("per_order"), "fixed_usd", "per_order"),
    )


def fees_for_condor(
    schedule: FeeSchedule,
    *,
    contracts_per_leg: int = 1,
    round_trip: bool = False,
) -> float:
    """Total USD fees for one iron condor under ``schedule``.

    Opening fees are ``4 legs x contracts_per_leg x (commission +
    regulatory) + fixed_per_order``. The default charges the opening side
    only: 0DTE condors expire at settlement, so there is no closing trade.
    ``round_trip=True`` doubles the total for strategies that close the
    position before expiry.
    """

    if contracts_per_leg < 1:
        raise FeeModelError("contracts_per_leg must be at least 1")
    opening = (
        CONDOR_LEG_COUNT
        * contracts_per_leg
        * (schedule.commission_per_contract_usd + schedule.regulatory_per_contract_usd)
        + schedule.fixed_per_order_usd
    )
    total = opening * 2 if round_trip else opening
    return _finite_non_negative(total, "condor fees")


def fee_adjusted_pnl(gross_pnl: float, fees: float) -> float:
    """Net PnL after fees: ``gross_pnl - fees``, in the caller's units.

    Both arguments must be finite and ``fees`` non-negative. When the gross
    PnL is in index points, convert USD fees first
    (``fees_usd / SPXW_POINT_VALUE_USD``) so the subtraction is coherent.
    """

    gross = float(gross_pnl)
    if not math.isfinite(gross):
        raise FeeModelError("gross_pnl must be finite")
    fee_total = _finite_non_negative(fees, "fees")
    net = gross - fee_total
    if not math.isfinite(net):
        raise FeeModelError("net pnl must be finite")
    return net
