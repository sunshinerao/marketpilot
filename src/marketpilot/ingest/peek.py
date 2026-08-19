from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import pandas as pd

from marketpilot.ingest.local_landing import (
    FilesystemEncryptedObjectStore,
    LocalAesGcmCipher,
)
from marketpilot.ingest.pit_ledger import PitBatchLedger

MAX_PREVIEW_PLAINTEXT_BYTES = 256 * 1024 * 1024


class PeekError(ValueError):
    """Raised when a landed batch cannot be previewed as requested."""


@dataclass(frozen=True, slots=True)
class LandedBatch:
    logical_key: str
    payload: bytes
    schema: str


def load_landed_batch(
    *,
    data_root: str | Path,
    pit_ledger_path: str | Path,
    logical_key: str,
) -> LandedBatch:
    """Resolve a landed batch through the PIT ledger and decrypt it in memory."""

    ledger = PitBatchLedger(pit_ledger_path)
    record = ledger.find(logical_key)
    if record is None:
        raise PeekError(f"no landed batch for {logical_key}")
    content = record.content()
    object_key = str(content["object_key"])
    dataset = str(content["dataset"])
    store = FilesystemEncryptedObjectStore(data_root)
    obj = store.get(object_key)
    if obj is None:
        raise PeekError(f"landing object missing for {logical_key}")
    landing_id = object_key.split("/")[-1]
    associated = f"marketpilot:{landing_id}:databento:{dataset}".encode()
    cipher = LocalAesGcmCipher(Path(data_root) / "_keys" / "local-aesgcm-v1.key")
    payload = cipher.decrypt(obj.envelope, associated_data=associated)
    return LandedBatch(
        logical_key=logical_key,
        payload=payload,
        schema=str(content["schema"]),
    )


def preview_csv(payload: bytes, *, limit: int) -> str:
    frame = pd.read_csv(StringIO(payload.decode("utf-8")), nrows=limit)
    return str(frame.to_string(index=False))


def preview_dbn(payload: bytes, *, limit: int, force: bool) -> str:
    """Decode a DBN day batch and format the first rows as a table."""

    if len(payload) > MAX_PREVIEW_PLAINTEXT_BYTES and not force:
        raise PeekError(
            f"batch is {len(payload) >> 20} MB; pass --force to decode large batches"
        )
    from databento import DBNStore  # imported lazily to keep startup light

    store = DBNStore.from_bytes(payload)
    frame = store.to_df()
    return str(frame.head(limit).to_string())


def preview_batch(batch: LandedBatch, *, limit: int, force: bool) -> str:
    if batch.schema == "definition":
        return preview_csv(batch.payload, limit=limit)
    return preview_dbn(batch.payload, limit=limit, force=force)
