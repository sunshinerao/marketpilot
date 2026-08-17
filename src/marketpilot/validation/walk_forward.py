from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True, slots=True)
class ValidationSample:
    sample_id: str
    observed_at: datetime
    group_id: str
    event_type: str
    regime: str

    def __post_init__(self) -> None:
        if not self.sample_id.strip() or not self.group_id.strip():
            raise ValueError("sample_id and group_id must not be blank")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_index: int
    train_ids: tuple[str, ...]
    test_ids: tuple[str, ...]
    purge_cutoff: datetime
    test_start: datetime
    test_end: datetime


class PurgedWalkForwardSplitter:
    """Expanding-window time split that keeps caller-defined groups in one fold."""

    def __init__(
        self,
        *,
        min_train_groups: int,
        test_groups: int,
        purge_gap: timedelta,
        step_groups: int | None = None,
    ) -> None:
        if min_train_groups <= 0 or test_groups <= 0:
            raise ValueError("train and test group counts must be positive")
        if purge_gap < timedelta(0):
            raise ValueError("purge_gap must not be negative")
        if step_groups is not None and step_groups <= 0:
            raise ValueError("step_groups must be positive")
        self._min_train_groups = min_train_groups
        self._test_groups = test_groups
        self._purge_gap = purge_gap
        self._step_groups = step_groups or test_groups

    def split(self, samples: Sequence[ValidationSample]) -> tuple[WalkForwardFold, ...]:
        if len({sample.sample_id for sample in samples}) != len(samples):
            raise ValueError("sample_id values must be unique")
        grouped: dict[str, list[ValidationSample]] = defaultdict(list)
        for sample in samples:
            grouped[sample.group_id].append(sample)
        ordered_groups = sorted(
            grouped,
            key=lambda group_id: (
                min(sample.observed_at for sample in grouped[group_id]),
                group_id,
            ),
        )

        folds: list[WalkForwardFold] = []
        test_offset = self._min_train_groups
        while test_offset + self._test_groups <= len(ordered_groups):
            test_group_ids = set(ordered_groups[test_offset : test_offset + self._test_groups])
            test_samples = sorted(
                (sample for group_id in test_group_ids for sample in grouped[group_id]),
                key=lambda sample: (sample.observed_at, sample.sample_id),
            )
            test_start = test_samples[0].observed_at
            purge_cutoff = test_start - self._purge_gap
            train_group_ids = set(ordered_groups[:test_offset])
            train_samples = sorted(
                (
                    sample
                    for group_id in train_group_ids
                    for sample in grouped[group_id]
                    if sample.observed_at < purge_cutoff
                ),
                key=lambda sample: (sample.observed_at, sample.sample_id),
            )
            if not train_samples:
                raise ValueError("purge gap leaves a fold without training samples")
            folds.append(
                WalkForwardFold(
                    fold_index=len(folds),
                    train_ids=tuple(sample.sample_id for sample in train_samples),
                    test_ids=tuple(sample.sample_id for sample in test_samples),
                    purge_cutoff=purge_cutoff,
                    test_start=test_start,
                    test_end=test_samples[-1].observed_at,
                )
            )
            test_offset += self._step_groups
        return tuple(folds)
