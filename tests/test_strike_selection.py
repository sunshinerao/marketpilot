import pytest

from marketpilot.models.strikepilot.strikes import max_loss_dollars, select_iron_condor_strikes


def test_strikes_round_outward_and_keep_asymmetric_distances() -> None:
    result = select_iron_condor_strikes(
        center=7812.4,
        up_tail=28.6,
        down_tail=34.2,
        joint_buffer=3.5,
    )
    assert (result.short_put, result.long_put) == (7770, 7765)
    assert (result.short_call, result.long_call) == (7845, 7850)
    assert result.put_distance == pytest.approx(42.4)
    assert result.call_distance == pytest.approx(32.6)


def test_max_loss_uses_conservative_credit_formula() -> None:
    assert max_loss_dollars(0.62) == pytest.approx(438.0)
