from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from helpers_ingest import land_fake_day

from marketpilot.cli import main
from marketpilot.ingest.peek import (
    MAX_PREVIEW_PLAINTEXT_BYTES,
    LandedBatch,
    PeekError,
    load_landed_batch,
    preview_batch,
    preview_csv,
)


def test_load_landed_batch_roundtrips_through_encryption(tmp_path: Path) -> None:
    logical_key = land_fake_day(tmp_path, date(2026, 8, 17))

    batch = load_landed_batch(
        data_root=tmp_path / "raw",
        pit_ledger_path=tmp_path / "pit" / "records.jsonl",
        logical_key=logical_key,
    )

    assert batch.payload == f"payload:{logical_key}".encode()
    assert batch.schema == "cbbo-1m"


def test_load_landed_batch_missing_key_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PeekError, match="no landed batch"):
        load_landed_batch(
            data_root=tmp_path / "raw",
            pit_ledger_path=tmp_path / "pit" / "records.jsonl",
            logical_key="OPRA.PILLAR/cbbo-1m/spxw-0dte/2026-08-17",
        )


def test_preview_csv_formats_a_table() -> None:
    table = preview_csv(b"symbol,strike\nSPXW  260817C06400000,6400\n", limit=5)
    assert "SPXW" in table
    assert "6400" in table


def test_large_dbn_batch_requires_force() -> None:
    batch = LandedBatch(
        logical_key="k",
        payload=b"x" * (MAX_PREVIEW_PLAINTEXT_BYTES + 1),
        schema="cbbo-1m",
    )
    with pytest.raises(PeekError, match="--force"):
        preview_batch(batch, limit=5, force=False)


def test_definition_schema_dispatches_to_csv() -> None:
    batch = LandedBatch(logical_key="k", payload=b"a,b\n1,2\n", schema="definition")
    assert "1" in preview_batch(batch, limit=5, force=False)


def test_ingest_peek_cli_missing_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "ingest-peek",
            "--logical-key",
            "OPRA.PILLAR/cbbo-1m/spxw-0dte/2026-08-17",
            "--data-root",
            str(tmp_path / "raw"),
            "--pit-ledger",
            str(tmp_path / "pit" / "records.jsonl"),
        ]
    )
    assert code == 2
    assert "no landed batch" in capsys.readouterr().out
