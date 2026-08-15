from __future__ import annotations

import math


def implied_spx_from_anchor(
    spx_anchor: float,
    futures_current: float,
    futures_anchor: float,
    carry_integral: float = 0.0,
) -> float:
    """Map an explicit ES contract to an implied cash coordinate.

    Both futures prices must refer to the same expiry and synchronized anchor. Contract
    validation belongs to the caller because this pure function receives only numbers.
    """

    if spx_anchor <= 0 or futures_current <= 0 or futures_anchor <= 0:
        raise ValueError("anchor and futures prices must be positive")
    return spx_anchor * (futures_current / futures_anchor) * math.exp(carry_integral)
