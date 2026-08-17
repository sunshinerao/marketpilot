from __future__ import annotations

import math
import tomllib
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from marketpilot.domain.snapshot import freeze_snapshot


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _finite(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class PromotionCriteria:
    criteria_id: str
    version: str
    registered_at: datetime
    required_slices: tuple[tuple[str, str], ...]
    minimum_samples_per_slice: int
    maximum_expiry_cross_rate: float
    maximum_touch_rate: float
    maximum_cvar: float
    maximum_drawdown: float
    minimum_no_trade_pnl_difference: float
    criteria_hash: str

    @classmethod
    def create(
        cls,
        *,
        criteria_id: str,
        version: str,
        registered_at: datetime,
        required_slices: tuple[tuple[str, str], ...],
        minimum_samples_per_slice: int,
        maximum_expiry_cross_rate: float,
        maximum_touch_rate: float,
        maximum_cvar: float,
        maximum_drawdown: float,
        minimum_no_trade_pnl_difference: float,
    ) -> PromotionCriteria:
        candidate = cls(
            criteria_id=criteria_id,
            version=version,
            registered_at=_aware_utc(registered_at, "registered_at"),
            required_slices=required_slices,
            minimum_samples_per_slice=minimum_samples_per_slice,
            maximum_expiry_cross_rate=_finite(
                maximum_expiry_cross_rate, "maximum_expiry_cross_rate"
            ),
            maximum_touch_rate=_finite(maximum_touch_rate, "maximum_touch_rate"),
            maximum_cvar=_finite(maximum_cvar, "maximum_cvar"),
            maximum_drawdown=_finite(maximum_drawdown, "maximum_drawdown"),
            minimum_no_trade_pnl_difference=_finite(
                minimum_no_trade_pnl_difference,
                "minimum_no_trade_pnl_difference",
            ),
            criteria_hash="",
        )
        candidate._validate_fields()
        return replace(candidate, criteria_hash=candidate._calculated_hash())

    def verify(self) -> None:
        self._validate_fields()
        if self.criteria_hash != self._calculated_hash():
            raise ValueError("promotion criteria hash mismatch")

    def _validate_fields(self) -> None:
        if not self.criteria_id.strip() or not self.version.strip():
            raise ValueError("criteria_id and version are required")
        _aware_utc(self.registered_at, "registered_at")
        if not self.required_slices or len(set(self.required_slices)) != len(
            self.required_slices
        ):
            raise ValueError("required_slices must be non-empty and unique")
        if any(not event.strip() or not regime.strip() for event, regime in self.required_slices):
            raise ValueError("required slice values must not be blank")
        if self.minimum_samples_per_slice <= 0:
            raise ValueError("minimum_samples_per_slice must be positive")
        for name, value in (
            ("maximum_expiry_cross_rate", self.maximum_expiry_cross_rate),
            ("maximum_touch_rate", self.maximum_touch_rate),
        ):
            if not 0 <= _finite(value, name) <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        for name, value in (
            ("maximum_cvar", self.maximum_cvar),
            ("maximum_drawdown", self.maximum_drawdown),
        ):
            if _finite(value, name) < 0:
                raise ValueError(f"{name} must not be negative")

    def _calculated_hash(self) -> str:
        payload = asdict(self)
        payload.pop("criteria_hash")
        return freeze_snapshot(payload).snapshot_id


@dataclass(frozen=True, slots=True)
class ValidationSliceEvidence:
    event_type: str
    regime: str
    sample_count: int
    expiry_cross_rate: float
    touch_rate: float
    cvar: float
    maximum_drawdown: float
    no_trade_pnl_difference: float

    def __post_init__(self) -> None:
        if not self.event_type.strip() or not self.regime.strip():
            raise ValueError("event_type and regime are required")
        if self.sample_count < 0:
            raise ValueError("sample_count must not be negative")
        for name in (
            "expiry_cross_rate",
            "touch_rate",
            "cvar",
            "maximum_drawdown",
            "no_trade_pnl_difference",
        ):
            value = _finite(getattr(self, name), name)
            object.__setattr__(self, name, value)
        if not 0 <= self.expiry_cross_rate <= 1 or not 0 <= self.touch_rate <= 1:
            raise ValueError("cross and touch rates must be in [0, 1]")
        if self.cvar < 0 or self.maximum_drawdown < 0:
            raise ValueError("cvar and maximum_drawdown must not be negative")

    @property
    def slice_key(self) -> tuple[str, str]:
        return self.event_type, self.regime


@dataclass(frozen=True, slots=True)
class FrozenValidationReport:
    criteria_hash: str
    data_manifest_hash: str
    holdout_manifest_hash: str
    evaluated_at: datetime
    slices: tuple[ValidationSliceEvidence, ...]
    passed: bool
    failures: tuple[str, ...]
    report_hash: str

    def verify(self) -> None:
        payload = asdict(self)
        payload.pop("report_hash")
        if self.report_hash != freeze_snapshot(payload).snapshot_id:
            raise ValueError("validation report hash mismatch")


def evaluate_promotion_gate(
    criteria: PromotionCriteria,
    *,
    data_manifest_hash: str,
    holdout_manifest_hash: str,
    holdout_frozen_at: datetime,
    evaluated_at: datetime,
    slices: tuple[ValidationSliceEvidence, ...],
) -> FrozenValidationReport:
    """Evaluate only a pre-registered, frozen holdout and return tamper-evident evidence."""

    criteria.verify()
    frozen_at = _aware_utc(holdout_frozen_at, "holdout_frozen_at")
    evaluated = _aware_utc(evaluated_at, "evaluated_at")
    if not data_manifest_hash.strip() or not holdout_manifest_hash.strip():
        raise ValueError("data and holdout manifest hashes are required")
    if frozen_at <= criteria.registered_at:
        raise ValueError("holdout must be frozen after criteria registration")
    if evaluated <= frozen_at:
        raise ValueError("evaluation must occur after the holdout is frozen")
    if len({item.slice_key for item in slices}) != len(slices):
        raise ValueError("validation slice keys must be unique")

    evidence = {item.slice_key: item for item in slices}
    failures: list[str] = []
    for event_type, regime in criteria.required_slices:
        prefix = f"{event_type}/{regime}"
        item = evidence.get((event_type, regime))
        if item is None:
            failures.append(f"{prefix}:MISSING_SLICE")
            continue
        if item.sample_count < criteria.minimum_samples_per_slice:
            failures.append(f"{prefix}:INSUFFICIENT_SAMPLES")
        if item.expiry_cross_rate > criteria.maximum_expiry_cross_rate:
            failures.append(f"{prefix}:EXPIRY_CROSS_EXCEEDED")
        if item.touch_rate > criteria.maximum_touch_rate:
            failures.append(f"{prefix}:TOUCH_RATE_EXCEEDED")
        if item.cvar > criteria.maximum_cvar:
            failures.append(f"{prefix}:CVAR_EXCEEDED")
        if item.maximum_drawdown > criteria.maximum_drawdown:
            failures.append(f"{prefix}:DRAWDOWN_EXCEEDED")
        if item.no_trade_pnl_difference < criteria.minimum_no_trade_pnl_difference:
            failures.append(f"{prefix}:NO_TRADE_EFFECT_BELOW_MINIMUM")

    candidate = FrozenValidationReport(
        criteria_hash=criteria.criteria_hash,
        data_manifest_hash=data_manifest_hash,
        holdout_manifest_hash=holdout_manifest_hash,
        evaluated_at=evaluated,
        slices=tuple(sorted(slices, key=lambda item: item.slice_key)),
        passed=not failures,
        failures=tuple(sorted(failures)),
        report_hash="",
    )
    payload = asdict(candidate)
    payload.pop("report_hash")
    report = replace(candidate, report_hash=freeze_snapshot(payload).snapshot_id)
    report.verify()
    return report


def load_promotion_criteria(path: str | Path) -> PromotionCriteria:
    """Load pre-registered thresholds from a versioned TOML artifact."""

    with Path(path).open("rb") as stream:
        raw = tomllib.load(stream)
    required = tuple(
        (str(item["event_type"]), str(item["regime"]))
        for item in raw["required_slices"]
    )
    return PromotionCriteria.create(
        criteria_id=str(raw["criteria_id"]),
        version=str(raw["version"]),
        registered_at=datetime.fromisoformat(str(raw["registered_at"]).replace("Z", "+00:00")),
        required_slices=required,
        minimum_samples_per_slice=int(raw["minimum_samples_per_slice"]),
        maximum_expiry_cross_rate=float(raw["maximum_expiry_cross_rate"]),
        maximum_touch_rate=float(raw["maximum_touch_rate"]),
        maximum_cvar=float(raw["maximum_cvar"]),
        maximum_drawdown=float(raw["maximum_drawdown"]),
        minimum_no_trade_pnl_difference=float(raw["minimum_no_trade_pnl_difference"]),
    )
