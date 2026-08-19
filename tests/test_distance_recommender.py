from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from marketpilot.validation.distance_recommender import recommend_walk_forward
from marketpilot.validation.tail_model import (
    EntryFeatures,
    ExcursionLabel,
    TailModelConfig,
    UnconditionalTailModel,
)

CONFIG = TailModelConfig(quantiles=(0.95, 0.975, 0.99))


def _labels(days: list[date], up: float = 10.0, down: float = 12.0) -> list[ExcursionLabel]:
    return [
        ExcursionLabel(day=day, up_max=up, down_max=down, entry_price=6400.0)
        for day in days
    ]


def _features(days: list[date], iv: float = 0.12) -> list[EntryFeatures]:
    return [EntryFeatures(day=day, atm_iv=iv, atm_iv_valid=True) for day in days]


def test_expanding_window_produces_only_out_of_sample_recommendations(
    tmp_path: Path,
) -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(80)]
    out = tmp_path / "distances.jsonl"
    report = recommend_walk_forward(
        labels=_labels(days),
        features=_features(days),
        model_factory=lambda: UnconditionalTailModel(config=CONFIG),
        quantile=0.975,
        min_train_days=60,
        out_path=out,
    )

    assert report.insufficient_history_days == 60
    assert report.recommended_days == 20
    assert report.abstain_days == 0
    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(records) == 20
    # First recommended day is the day after the 60th training day.
    assert records[0]["day"] == days[60].isoformat()
    assert records[0]["regime"] == "ALL"
    assert records[0]["down_distance"] >= 12.0
    assert records[0]["up_distance"] >= 10.0


def test_recommendation_uses_only_prior_days(tmp_path: Path) -> None:
    # A huge excursion on the last day must not change the last day's own
    # recommendation (fitted strictly before it).
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(70)]
    labels = _labels(days)
    labels[-1] = replace(labels[-1], up_max=10_000.0, down_max=10_000.0)
    out = tmp_path / "distances.jsonl"
    recommend_walk_forward(
        labels=labels,
        features=_features(days),
        model_factory=lambda: UnconditionalTailModel(config=CONFIG),
        quantile=0.975,
        min_train_days=60,
        out_path=out,
    )
    records = [json.loads(line) for line in out.read_text().splitlines()]
    last = records[-1]
    assert last["day"] == days[-1].isoformat()
    assert last["down_distance"] < 100.0  # would be ~10000 if the day leaked in
