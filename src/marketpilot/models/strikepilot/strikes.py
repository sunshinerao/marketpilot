from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IronCondorStrikes:
    short_put: int
    long_put: int
    short_call: int
    long_call: int
    put_distance: float
    call_distance: float


def select_iron_condor_strikes(
    *,
    center: float,
    up_tail: float,
    down_tail: float,
    joint_buffer: float = 0.0,
    up_buffer: float = 0.0,
    down_buffer: float = 0.0,
    strike_increment: int = 5,
    wing_width: int = 5,
) -> IronCondorStrikes:
    if center <= 0:
        raise ValueError("center must be positive")
    if min(up_tail, down_tail, joint_buffer, up_buffer, down_buffer) < 0:
        raise ValueError("tails and buffers must be non-negative")
    if strike_increment <= 0 or wing_width <= 0:
        raise ValueError("strike increment and wing width must be positive")

    call_distance = up_tail + joint_buffer + up_buffer
    put_distance = down_tail + joint_buffer + down_buffer
    short_call = strike_increment * math.ceil((center + call_distance) / strike_increment)
    short_put = strike_increment * math.floor((center - put_distance) / strike_increment)
    return IronCondorStrikes(
        short_put=short_put,
        long_put=short_put - wing_width,
        short_call=short_call,
        long_call=short_call + wing_width,
        put_distance=round(center - short_put, 10),
        call_distance=round(short_call - center, 10),
    )


def max_loss_dollars(net_credit: float, wing_width: int = 5, multiplier: int = 100) -> float:
    if not 0 <= net_credit < wing_width:
        raise ValueError("net credit must be between zero and wing width")
    return (wing_width - net_credit) * multiplier
