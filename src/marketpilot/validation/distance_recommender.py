from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from marketpilot.validation.tail_distances import TailDistances
from marketpilot.validation.tail_model import (
    EntryFeatures,
    ExcursionLabel,
    TailModel,
)


@dataclass(frozen=True, slots=True)
class DistanceRecommendationReport:
    out_path: Path
    recommended_days: int
    abstain_days: int
    insufficient_history_days: int
    min_train_days: int
    quantile: float

    def to_dict(self) -> dict[str, object]:
        return {
            "out_path": str(self.out_path),
            "recommended_days": self.recommended_days,
            "abstain_days": self.abstain_days,
            "insufficient_history_days": self.insufficient_history_days,
            "min_train_days": self.min_train_days,
            "quantile": self.quantile,
        }


def recommend_walk_forward(
    *,
    labels: Sequence[ExcursionLabel],
    features: Sequence[EntryFeatures],
    model_factory: Callable[[], TailModel],
    quantile: float,
    min_train_days: int = 60,
    purge_gap: timedelta = timedelta(days=1),
    out_path: Path,
) -> DistanceRecommendationReport:
    """Emit per-day out-of-sample tail distances with an expanding window.

    Each day's recommendation comes from a model fitted only on days strictly
    before (day - purge_gap), so no label from the evaluated day or the embargo
    window can influence it. Days the model abstains on are NO_TRADE by
    definition and produce no record.
    """

    labels_by_day = {label.day: label for label in labels}
    features_by_day = {feature.day: feature for feature in features}
    days = sorted(labels_by_day)
    recommendations: list[TailDistances] = []
    abstain_days = 0
    insufficient_history_days = 0
    for day in days:
        cutoff = day - purge_gap
        train_days = [candidate for candidate in days if candidate <= cutoff]
        if len(train_days) < min_train_days:
            insufficient_history_days += 1
            continue
        model = model_factory().fit(
            [labels_by_day[candidate] for candidate in train_days],
            [
                features_by_day.get(
                    candidate,
                    EntryFeatures(day=candidate, atm_iv=None, atm_iv_valid=False),
                )
                for candidate in train_days
            ],
        )
        feature = features_by_day.get(
            day, EntryFeatures(day=day, atm_iv=None, atm_iv_valid=False)
        )
        recommendation = model.recommend(feature, quantile)
        if recommendation is None:
            abstain_days += 1
            continue
        recommendations.append(recommendation)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for recommendation in recommendations:
            record = asdict(recommendation)
            record["day"] = recommendation.day.isoformat()
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return DistanceRecommendationReport(
        out_path=out_path,
        recommended_days=len(recommendations),
        abstain_days=abstain_days,
        insufficient_history_days=insufficient_history_days,
        min_train_days=min_train_days,
        quantile=quantile,
    )
