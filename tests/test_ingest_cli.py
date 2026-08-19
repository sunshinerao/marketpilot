from __future__ import annotations

import json
from pathlib import Path

import pytest

from marketpilot.cli import _default_pulls, _landing_service, _parser, main

WINDOW = ["--start", "2026-08-14", "--end", "2026-08-17"]


def test_ingest_plan_requires_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    assert main(["ingest-plan", *WINDOW]) == 2
    assert "DATABENTO_API_KEY" in capsys.readouterr().out


def test_ingest_run_refuses_without_confirm_spend(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABENTO_API_KEY", "test-key")
    # Refusal happens before any provider call, including cost estimation.
    assert main(["ingest-run", *WINDOW]) == 2
    assert "confirm-spend" in capsys.readouterr().out


def test_ingest_plan_rejects_window_without_trading_days(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABENTO_API_KEY", "test-key")
    # 2026-08-15/16 is a weekend.
    assert main(["ingest-plan", "--start", "2026-08-15", "--end", "2026-08-16"]) == 2
    assert "no verified trading days" in capsys.readouterr().out


def test_ingest_audit_reports_missing_days(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "ingest-audit",
            *WINDOW,
            "--pit-ledger",
            str(tmp_path / "empty.jsonl"),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["status"] == "FAIL"
    assert out["expected_trading_days"] == 2
    assert out["missing_trading_days"] == ["2026-08-14", "2026-08-17"]


def test_default_pulls_cover_chain_and_front_month() -> None:
    from datetime import date

    pulls = _default_pulls([date(2026, 8, 17)], strategy="whole-chain")
    assert len(pulls) == 2
    by_dataset = {pull.dataset: pull for pull in pulls}
    assert by_dataset["OPRA.PILLAR"].symbols == ("SPXW.OPT",)
    assert by_dataset["OPRA.PILLAR"].stype_in == "parent"
    assert by_dataset["OPRA.PILLAR"].scope == "spxw-whole-chain"
    assert by_dataset["GLBX.MDP3"].symbols == ("ES.v.0",)
    assert by_dataset["GLBX.MDP3"].stype_in == "continuous"


def test_default_pulls_0dte_strategy_uses_raw_symbol_placeholder() -> None:
    from datetime import date

    pulls = _default_pulls([date(2026, 8, 17)], strategy="0dte")
    assert len(pulls) == 2
    by_dataset = {pull.dataset: pull for pull in pulls}
    spxw = by_dataset["OPRA.PILLAR"]
    assert spxw.stype_in == "raw_symbol"
    assert spxw.scope == "spxw-0dte"
    assert spxw.symbols == ("SPXW.OPT",)  # replaced by chain resolution at plan time
    # The ES continuous pull is unchanged across strategies.
    assert by_dataset["GLBX.MDP3"].symbols == ("ES.v.0",)
    assert by_dataset["GLBX.MDP3"].stype_in == "continuous"


def test_default_pulls_rejects_unknown_strategy() -> None:
    from datetime import date

    with pytest.raises(ValueError, match="unknown strategy"):
        _default_pulls([date(2026, 8, 17)], strategy="everything")


def test_strategy_flag_defaults_to_0dte_and_accepts_whole_chain() -> None:
    base = ["ingest-plan", *WINDOW]
    assert _parser().parse_args(base).strategy == "0dte"
    assert _parser().parse_args([*base, "--strategy", "whole-chain"]).strategy == "whole-chain"
    run_args = _parser().parse_args(["ingest-run", *WINDOW, "--confirm-spend"])
    assert run_args.strategy == "0dte"


def test_landing_service_constructs_with_local_key(tmp_path: Path) -> None:
    service = _landing_service(str(tmp_path / "raw"))
    assert service is not None
    key_file = tmp_path / "raw" / "_keys" / "local-aesgcm-v1.key"
    assert key_file.exists()
    assert oct(key_file.stat().st_mode & 0o777) == "0o600"


def test_calibrate_labels_prints_outcome_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import date

    import marketpilot.features.implied_spx as implied_spx
    import marketpilot.validation.excursion_batch as excursion_batch
    from marketpilot.validation.excursion_batch import DayOutcome, LabelBatchReport

    monkeypatch.setattr(
        implied_spx,
        "load_anchor_closes",
        lambda start, end: {date(2026, 8, 14): 6400.0},
    )

    def fake_generate_labels(**kwargs: object) -> LabelBatchReport:
        return LabelBatchReport(
            start=kwargs["start"],  # type: ignore[arg-type]
            end=kwargs["end"],  # type: ignore[arg-type]
            labels_path=Path(str(kwargs["labels_path"])),
            outcomes=(
                DayOutcome(date(2026, 8, 17), "LABELLED"),
                DayOutcome(date(2026, 8, 18), "GAP"),
            ),
        )

    monkeypatch.setattr(excursion_batch, "generate_labels", fake_generate_labels)

    code = main(
        [
            "calibrate-labels",
            "--start",
            "2026-08-17",
            "--end",
            "2026-08-18",
            "--data-root",
            str(tmp_path / "raw"),
            "--pit-ledger",
            str(tmp_path / "pit.jsonl"),
            "--labels",
            str(tmp_path / "labels.jsonl"),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["status"] == "OK"
    assert out["counts"] == {"GAP": 1, "LABELLED": 1}
    assert out["labels"] == str(tmp_path / "labels.jsonl")


def test_calibrate_labels_fails_when_anchors_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import marketpilot.features.implied_spx as implied_spx
    from marketpilot.features.implied_spx import AnchorCloseError

    def raise_anchor_error(start: object, end: object) -> dict[object, float]:
        raise AnchorCloseError("no official SPX closes available")

    monkeypatch.setattr(implied_spx, "load_anchor_closes", raise_anchor_error)

    code = main(["calibrate-labels", "--start", "2026-08-17", "--end", "2026-08-18"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["status"] == "FAIL"
    assert "SPX" in out["reason"]
