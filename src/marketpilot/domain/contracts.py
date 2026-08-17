from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_ES_SYMBOL = re.compile(r"^ES([HMUZ])(\d{1,4})$")
_MONTH_BY_CODE = {"H": 3, "M": 6, "U": 9, "Z": 12}
_CONTINUOUS_ALIASES = {"ES", "/ES", "ESMAIN", "ES-MAIN", "ES1!", "ES.C.0"}


def normalize_explicit_es_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if normalized in _CONTINUOUS_ALIASES or "MAIN" in normalized:
        raise ValueError("continuous/main ES contracts are forbidden")
    if _ES_SYMBOL.fullmatch(normalized) is None:
        raise ValueError("ES symbol must be an explicit quarterly contract, for example ESU6")
    return normalized


@dataclass(frozen=True, slots=True)
class ExplicitESContract:
    symbol: str
    expiry: date

    def __post_init__(self) -> None:
        normalized = normalize_explicit_es_symbol(self.symbol)
        match = _ES_SYMBOL.fullmatch(normalized)
        if match is None:  # pragma: no cover - normalize already enforces this
            raise ValueError("invalid ES symbol")
        if self.expiry.month != _MONTH_BY_CODE[match.group(1)]:
            raise ValueError("ES symbol month code does not match the explicit expiry date")
        year_digits = match.group(2)
        if not str(self.expiry.year).endswith(year_digits):
            raise ValueError("ES symbol year does not match the explicit expiry date")
        object.__setattr__(self, "symbol", normalized)
