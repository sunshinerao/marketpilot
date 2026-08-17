from datetime import date

import pytest

from marketpilot.domain.contracts import ExplicitESContract, normalize_explicit_es_symbol


@pytest.mark.parametrize("symbol", ["ES", "/ES", "ESmain", "ES-MAIN", "ES1!", "ES.C.0"])
def test_continuous_es_contract_aliases_are_rejected(symbol: str) -> None:
    with pytest.raises(ValueError, match="continuous/main"):
        normalize_explicit_es_symbol(symbol)


def test_explicit_contract_carries_expiry_and_normalizes_symbol() -> None:
    contract = ExplicitESContract(" esu6 ", date(2026, 9, 18))

    assert contract.symbol == "ESU6"
    assert contract.expiry == date(2026, 9, 18)


def test_explicit_contract_rejects_invalid_or_mismatched_expiry() -> None:
    with pytest.raises(ValueError, match="quarterly contract"):
        ExplicitESContract("ESABC", date(2026, 9, 18))
    with pytest.raises(ValueError, match="does not match"):
        ExplicitESContract("ESU6", date(2026, 12, 18))
    with pytest.raises(ValueError, match="year does not match"):
        ExplicitESContract("ESU6", date(2027, 9, 17))
