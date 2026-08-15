import pytest

from marketpilot.features.basis import implied_spx_from_anchor


def test_implied_spx_uses_relative_same_contract_move() -> None:
    result = implied_spx_from_anchor(7785.76, 7802.5, 7805.0)
    assert result == pytest.approx(7783.2665, abs=0.001)


def test_implied_spx_rejects_non_positive_prices() -> None:
    with pytest.raises(ValueError):
        implied_spx_from_anchor(7785.76, 0, 7805.0)
