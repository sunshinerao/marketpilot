from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from marketpilot.validation.execution_economics import (
    ExecutionAssumptions,
    ExecutionFailure,
    LegNbbo,
    PnlMarkQuotes,
    conservative_executable_pnl,
    evaluate_conservative_pnl_path,
    value_opening_execution,
)

OPEN = datetime(2026, 8, 14, 14, 30, tzinfo=UTC)
ASSUMPTIONS = ExecutionAssumptions(
    max_quote_age=timedelta(seconds=2),
    fee_per_contract=0.50,
    slippage_per_contract=0.05,
    max_size_participation=0.5,
)


def quote(
    leg_id: str,
    quantity: int,
    bid: float,
    ask: float,
    *,
    at: datetime = OPEN,
    bid_size: int | None = 10,
    ask_size: int | None = 10,
) -> LegNbbo:
    return LegNbbo(
        leg_id=leg_id,
        quantity=quantity,
        multiplier=100.0,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        quoted_at=at,
    )


def test_conservative_credit_includes_nbbo_spread_slippage_fees_and_size() -> None:
    valued = value_opening_execution(
        (
            quote("short", -1, 5.00, 5.20),
            quote("long", 1, 2.00, 2.20),
        ),
        valued_at=OPEN + timedelta(seconds=1),
        assumptions=ASSUMPTIONS,
    )

    assert valued.is_executable is True
    assert [leg.execution_price for leg in valued.legs] == pytest.approx([4.95, 2.25])
    assert valued.gross_cashflow == pytest.approx(270.0)
    assert valued.fees == pytest.approx(1.0)
    assert valued.net_cashflow == pytest.approx(269.0)
    assert valued.net_credit == pytest.approx(269.0)
    assert sum(leg.half_spread_cost for leg in valued.legs) == pytest.approx(20.0)
    assert sum(leg.slippage_cost for leg in valued.legs) == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("changes", "failure"),
    [
        ({"at": OPEN - timedelta(seconds=3)}, ExecutionFailure.STALE_NBBO),
        ({"ask_size": None}, ExecutionFailure.MISSING_SIZE),
        ({"ask_size": 0}, ExecutionFailure.INSUFFICIENT_SIZE),
        ({"ask_size": 1}, ExecutionFailure.LIQUIDITY_LIMIT),
    ],
)
def test_unusable_nbbo_fails_closed_without_credit_or_leg_prices(
    changes: dict[str, object], failure: ExecutionFailure
) -> None:
    kwargs = {
        "at": OPEN,
        "bid_size": 10,
        "ask_size": 10,
    }
    kwargs.update(changes)
    leg = quote("long", 1, 2.0, 2.2, **kwargs)  # type: ignore[arg-type]
    valued = value_opening_execution(
        (leg,),
        valued_at=OPEN + timedelta(seconds=1),
        assumptions=ASSUMPTIONS,
    )

    assert valued.is_executable is False
    assert failure in valued.failures
    assert valued.legs == ()
    assert valued.gross_cashflow is None
    assert valued.net_cashflow is None
    assert valued.net_credit is None


def test_conservative_executable_pnl_uses_opposite_nbbo_sides_on_close() -> None:
    close = OPEN + timedelta(hours=1)
    result = conservative_executable_pnl(
        (
            quote("short", -1, 5.00, 5.20),
            quote("long", 1, 2.00, 2.20),
        ),
        (
            quote("short", -1, 3.00, 3.20, at=close),
            quote("long", 1, 1.00, 1.20, at=close),
        ),
        opened_at=OPEN,
        closed_at=close,
        assumptions=ASSUMPTIONS,
    )

    assert result is not None
    # Open +269; close: buy short -325, sell long +95, then $1 fees.
    assert result.closing_net_cashflow == pytest.approx(-231.0)
    assert result.pnl == pytest.approx(38.0)


def test_pnl_is_suppressed_for_leg_mismatch_or_stale_close() -> None:
    close = OPEN + timedelta(hours=1)
    opening = (quote("short", -1, 5.0, 5.2),)
    assert (
        conservative_executable_pnl(
            opening,
            (quote("different", -1, 3.0, 3.2, at=close),),
            opened_at=OPEN,
            closed_at=close,
            assumptions=ASSUMPTIONS,
        )
        is None
    )
    assert (
        conservative_executable_pnl(
            opening,
            (quote("short", -1, 3.0, 3.2, at=close - timedelta(seconds=3)),),
            opened_at=OPEN,
            closed_at=close,
            assumptions=ASSUMPTIONS,
        )
        is None
    )


def test_nonfinite_quote_or_economic_assumption_is_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        quote("bad", 1, float("nan"), 2.0)
    with pytest.raises(ValueError, match="finite"):
        ExecutionAssumptions(timedelta(seconds=1), float("inf"), 0.0)


def test_executable_pnl_path_computes_maximum_mtm_loss_and_fails_on_gaps() -> None:
    one = OPEN + timedelta(minutes=30)
    two = OPEN + timedelta(minutes=60)
    opening = (quote("short", -1, 5.0, 5.2),)
    path = evaluate_conservative_pnl_path(
        opening,
        (
            PnlMarkQuotes(one, (quote("short", -1, 5.5, 5.7, at=one),)),
            PnlMarkQuotes(two, (quote("short", -1, 4.0, 4.2, at=two),)),
        ),
        opened_at=OPEN,
        assumptions=ASSUMPTIONS,
    )
    assert path is not None
    assert [mark.pnl for mark in path.marks] == pytest.approx([-81.0, 69.0])
    assert path.maximum_mtm_loss == pytest.approx(81.0)

    stale = PnlMarkQuotes(
        two,
        (quote("short", -1, 4.0, 4.2, at=two - timedelta(seconds=3)),),
    )
    assert (
        evaluate_conservative_pnl_path(opening, (stale,), opened_at=OPEN, assumptions=ASSUMPTIONS)
        is None
    )
