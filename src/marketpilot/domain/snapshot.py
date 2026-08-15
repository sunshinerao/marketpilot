from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"Unsupported snapshot value: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class FrozenSnapshot:
    snapshot_id: str
    canonical_json: str


def freeze_snapshot(payload: Mapping[str, Any]) -> FrozenSnapshot:
    canonical = json.dumps(
        payload,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return FrozenSnapshot(snapshot_id=f"sha256:{digest}", canonical_json=canonical)
