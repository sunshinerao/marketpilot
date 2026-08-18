from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from marketpilot.services.raw_landing import (
    EncryptedLandingObject,
    EncryptionEnvelope,
    LandingContext,
    LandingReceipt,
)

LOCAL_KEY_ID = "local-aesgcm-v1"
LOCAL_ALGORITHM = "AES-256-GCM"


class LocalAesGcmCipher:
    """Development cipher backed by a local key file; production uses KMS/HSM."""

    def __init__(self, key_path: str | Path) -> None:
        self._key_path = Path(key_path)
        if self._key_path.exists():
            key = self._key_path.read_bytes()
        else:
            key = os.urandom(32)
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            self._key_path.write_bytes(key)
            self._key_path.chmod(0o600)
        if len(key) != 32:
            raise ValueError("local landing key must be 32 bytes")
        self._aesgcm = AESGCM(key)

    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> EncryptionEnvelope:
        nonce = os.urandom(12)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, associated_data)
        return EncryptionEnvelope(
            ciphertext=ciphertext,
            key_id=LOCAL_KEY_ID,
            algorithm=LOCAL_ALGORITHM,
            nonce=nonce,
        )

    def decrypt(self, envelope: EncryptionEnvelope, *, associated_data: bytes) -> bytes:
        if envelope.key_id != LOCAL_KEY_ID:
            raise ValueError(f"unknown landing key: {envelope.key_id}")
        return self._aesgcm.decrypt(envelope.nonce, envelope.ciphertext, associated_data)


class FilesystemEncryptedObjectStore:
    """Content-addressed encrypted object files under an ignored data directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _path_for(self, object_key: str) -> Path:
        return self._root / f"{object_key}.json"

    @staticmethod
    def _serialize(value: EncryptedLandingObject) -> str:
        return json.dumps(
            {
                "object_key": value.object_key,
                "content_type": value.content_type,
                "plaintext_sha256": value.plaintext_sha256,
                "key_id": value.envelope.key_id,
                "algorithm": value.envelope.algorithm,
                "nonce_b64": base64.b64encode(value.envelope.nonce).decode("ascii"),
                "ciphertext_b64": base64.b64encode(value.envelope.ciphertext).decode("ascii"),
            },
            sort_keys=True,
        )

    @staticmethod
    def _deserialize(raw: str) -> EncryptedLandingObject:
        data = json.loads(raw)
        return EncryptedLandingObject(
            object_key=str(data["object_key"]),
            envelope=EncryptionEnvelope(
                ciphertext=base64.b64decode(data["ciphertext_b64"]),
                key_id=str(data["key_id"]),
                algorithm=str(data["algorithm"]),
                nonce=base64.b64decode(data["nonce_b64"]),
            ),
            content_type=str(data["content_type"]),
            plaintext_sha256=str(data["plaintext_sha256"]),
        )

    def put_if_absent(self, value: EncryptedLandingObject) -> EncryptedLandingObject:
        destination = self._path_for(value.object_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        serialized = self._serialize(value)
        try:
            with destination.open("x", encoding="utf-8") as handle:
                handle.write(serialized)
            return value
        except FileExistsError:
            existing = self.get(value.object_key)
            assert existing is not None  # the file exists by definition of the race
            return existing

    def get(self, object_key: str) -> EncryptedLandingObject | None:
        destination = self._path_for(object_key)
        if not destination.exists():
            return None
        return self._deserialize(destination.read_text(encoding="utf-8"))


class JsonlLandingMetadataSink:
    """Append-only JSONL receipt sink; receipts never contain payload material."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def append(self, receipt: LandingReceipt) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = asdict(receipt)
        for field_name, value in record.items():
            if isinstance(value, datetime):
                record[field_name] = value.isoformat()
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


class StaticLandingAuthorizer:
    """Allow-list of (principal, purpose) pairs permitted to use the landing."""

    def __init__(self, allowed: frozenset[tuple[str, str]]) -> None:
        self._allowed = allowed

    def authorize(self, context: LandingContext) -> bool:
        return (context.principal, context.purpose) in self._allowed
