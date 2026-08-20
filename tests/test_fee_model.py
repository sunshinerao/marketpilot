"""Hermetic tests for validation/fee_model.py — TOML written to tmp_path only.

No data/raw, no network: the only repo file read is the versioned
``config/fees-v1.toml`` itself, and all mutation tests write their own
schedule files into ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marketpilot.validation.fee_model import (
    CONDOR_LEG_COUNT,
    EXPECTED_VERSION,
    SPXW_POINT_VALUE_USD,
    FeeModelError,
    FeeSchedule,
    fee_adjusted_pnl,
    fees_for_condor,
    load_fee_schedule,
)

REPO_FEES = Path(__file__).resolve().parent.parent / "config" / "fees-v1.toml"

SCHEDULE = FeeSchedule(
    version=EXPECTED_VERSION,
    commission_per_contract_usd=0.65,
    regulatory_per_contract_usd=0.05,
    fixed_per_order_usd=0.0,
)


def _write_schedule(tmp_path: Path, body: str, name: str = "fees.toml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


_VALID_BODY = """version = "fees-v1"

[per_contract]
commission_usd = 0.65
regulatory_usd = 0.05

[per_order]
fixed_usd = 0.0
"""


# --- shipped config -------------------------------------------------------


def test_shipped_fees_v1_config_loads_with_documented_defaults() -> None:
    schedule = load_fee_schedule(REPO_FEES)
    assert schedule.version == "fees-v1"
    # Conservative estimates: full retail commission, no commission-free
    # SPXW offer assumed, no fixed ticket fee.
    assert schedule.commission_per_contract_usd == pytest.approx(0.65)
    assert schedule.regulatory_per_contract_usd == pytest.approx(0.05)
    assert schedule.fixed_per_order_usd == pytest.approx(0.0)


# --- TOML validation ------------------------------------------------------


def test_load_fee_schedule_round_trips_valid_file(tmp_path: Path) -> None:
    schedule = load_fee_schedule(_write_schedule(tmp_path, _VALID_BODY))
    assert schedule == SCHEDULE


def test_load_fee_schedule_rejects_negative_fees(tmp_path: Path) -> None:
    bad = _write_schedule(tmp_path, _VALID_BODY.replace("commission_usd = 0.65",
                                                        "commission_usd = -0.65"))
    with pytest.raises(FeeModelError, match="must not be negative"):
        load_fee_schedule(bad)
    bad_reg = _write_schedule(tmp_path, _VALID_BODY.replace("regulatory_usd = 0.05",
                                                            "regulatory_usd = -0.01"),
                              name="neg-reg.toml")
    with pytest.raises(FeeModelError, match="must not be negative"):
        load_fee_schedule(bad_reg)


def test_load_fee_schedule_rejects_wrong_version(tmp_path: Path) -> None:
    bad = _write_schedule(tmp_path, _VALID_BODY.replace('"fees-v1"', '"fees-v2"'))
    with pytest.raises(FeeModelError, match="version"):
        load_fee_schedule(bad)
    missing = _write_schedule(tmp_path, _VALID_BODY.replace('version = "fees-v1"\n', ""),
                              name="no-version.toml")
    with pytest.raises(FeeModelError, match="version"):
        load_fee_schedule(missing)


def test_load_fee_schedule_rejects_missing_and_non_numeric_keys(tmp_path: Path) -> None:
    missing = _write_schedule(tmp_path, _VALID_BODY.replace("regulatory_usd = 0.05\n", ""))
    with pytest.raises(FeeModelError, match="regulatory_usd"):
        load_fee_schedule(missing)
    non_numeric = _write_schedule(tmp_path, _VALID_BODY.replace(
        "commission_usd = 0.65", 'commission_usd = "free"'), name="non-numeric.toml")
    with pytest.raises(FeeModelError, match="numeric"):
        load_fee_schedule(non_numeric)


def test_load_fee_schedule_rejects_malformed_toml(tmp_path: Path) -> None:
    bad = _write_schedule(tmp_path, "version = [not valid toml")
    with pytest.raises(FeeModelError, match="malformed"):
        load_fee_schedule(bad)


def test_fee_schedule_constructor_is_fail_closed() -> None:
    with pytest.raises(FeeModelError, match="version"):
        FeeSchedule(
            version="fees-v9",
            commission_per_contract_usd=0.65,
            regulatory_per_contract_usd=0.05,
        )
    with pytest.raises(FeeModelError, match="must not be negative"):
        FeeSchedule(
            version=EXPECTED_VERSION,
            commission_per_contract_usd=0.65,
            regulatory_per_contract_usd=0.05,
            fixed_per_order_usd=-1.0,
        )
    with pytest.raises(FeeModelError, match="finite"):
        FeeSchedule(
            version=EXPECTED_VERSION,
            commission_per_contract_usd=float("nan"),
            regulatory_per_contract_usd=0.05,
        )


# --- condor fee arithmetic ------------------------------------------------


def test_fees_for_condor_four_legs_opening_only() -> None:
    # 4 legs x 1 contract x (0.65 + 0.05) + 0 fixed = 2.80 USD.
    assert CONDOR_LEG_COUNT == 4
    assert fees_for_condor(SCHEDULE) == pytest.approx(2.80)


def test_fees_for_condor_scales_with_contracts_and_fixed_fee() -> None:
    schedule = FeeSchedule(
        version=EXPECTED_VERSION,
        commission_per_contract_usd=0.50,
        regulatory_per_contract_usd=0.10,
        fixed_per_order_usd=1.00,
    )
    # 4 legs x 3 contracts x 0.60 + 1.00 = 8.20 USD.
    assert fees_for_condor(schedule, contracts_per_leg=3) == pytest.approx(8.20)
    with pytest.raises(FeeModelError, match="at least 1"):
        fees_for_condor(schedule, contracts_per_leg=0)


def test_fees_for_condor_round_trip_doubles_opening() -> None:
    opening = fees_for_condor(SCHEDULE)
    assert fees_for_condor(SCHEDULE, round_trip=True) == pytest.approx(2 * opening)
    # Default is opening-only: 0DTE condors expire without closing trades.
    assert fees_for_condor(SCHEDULE) == fees_for_condor(SCHEDULE, round_trip=False)


# --- net PnL ---------------------------------------------------------------


def test_fee_adjusted_pnl_subtracts_fees() -> None:
    assert fee_adjusted_pnl(1.80, 0.028) == pytest.approx(1.772)
    assert fee_adjusted_pnl(-3.20, 0.028) == pytest.approx(-3.228)
    assert fee_adjusted_pnl(1.80, 0.0) == pytest.approx(1.80)


def test_fee_adjusted_pnl_validates_inputs() -> None:
    with pytest.raises(FeeModelError, match="must not be negative"):
        fee_adjusted_pnl(1.0, -0.01)
    with pytest.raises(FeeModelError, match="finite"):
        fee_adjusted_pnl(float("inf"), 0.01)


def test_point_value_conversion_matches_spxw_multiplier() -> None:
    # 2.80 USD of fees at 100 USD/point is a 0.028-point drag.
    assert SPXW_POINT_VALUE_USD == 100.0
    assert fees_for_condor(SCHEDULE) / SPXW_POINT_VALUE_USD == pytest.approx(0.028)
