from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class LandingAccessDenied(PermissionError):
    """Raised without payload or credential detail when landing access is denied."""


class LandingOperation(StrEnum):
    WRITE = "WRITE"
    READ = "READ"


@dataclass(frozen=True, slots=True)
class LandingContext:
    principal: str
    purpose: str
    operation: LandingOperation
    provider: str
    dataset: str


@dataclass(frozen=True, slots=True)
class EncryptionEnvelope:
    ciphertext: bytes = field(repr=False)
    key_id: str
    algorithm: str
    nonce: bytes = field(repr=False)

    def __repr__(self) -> str:
        return (
            "EncryptionEnvelope(ciphertext=<redacted>, "
            f"key_id={self.key_id!r}, algorithm={self.algorithm!r}, nonce=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class SensitivePayload:
    """Opaque raw payload wrapper whose representation is always redacted."""

    _value: bytes = field(repr=False)

    def __repr__(self) -> str:
        return "SensitivePayload(<redacted>)"

    def bytes_for_authorized_processing(self) -> bytes:
        """Reveal bytes only inside an already-authorized processing boundary."""

        return self._value


@dataclass(frozen=True, slots=True)
class EncryptedLandingObject:
    object_key: str
    envelope: EncryptionEnvelope = field(repr=False)
    content_type: str
    plaintext_sha256: str

    def __repr__(self) -> str:
        return (
            f"EncryptedLandingObject(object_key={self.object_key!r}, "
            "envelope=<redacted>, "
            f"content_type={self.content_type!r}, plaintext_sha256={self.plaintext_sha256!r})"
        )


@dataclass(frozen=True, slots=True)
class LandingReceipt:
    landing_id: str
    object_key: str
    provider: str
    dataset: str
    logical_key_hash: str
    published_at: datetime
    first_seen_at: datetime
    plaintext_sha256: str
    key_id: str
    algorithm: str
    content_type: str


class LandingAuthorizer(Protocol):
    def authorize(self, context: LandingContext) -> bool: ...


class PayloadCipher(Protocol):
    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> EncryptionEnvelope: ...

    def decrypt(self, envelope: EncryptionEnvelope, *, associated_data: bytes) -> bytes: ...


class EncryptedObjectStore(Protocol):
    def put_if_absent(self, value: EncryptedLandingObject) -> EncryptedLandingObject:
        """Atomically return the inserted object or the immutable existing winner."""
        ...

    def get(self, object_key: str) -> EncryptedLandingObject | None: ...


class LandingMetadataSink(Protocol):
    def append(self, receipt: LandingReceipt) -> None: ...


class LicensedPayloadLandingService:
    """Authorized encryption boundary for raw licensed payloads.

    Only an encrypted object store sees ciphertext. The metadata sink receives a safe
    receipt containing hashes and provenance, never plaintext, ciphertext, or nonce.
    Calling code must keep this service outside HTTP response and application-log paths.
    """

    def __init__(
        self,
        *,
        authorizer: LandingAuthorizer,
        cipher: PayloadCipher,
        object_store: EncryptedObjectStore,
        metadata_sink: LandingMetadataSink,
    ) -> None:
        self._authorizer = authorizer
        self._cipher = cipher
        self._object_store = object_store
        self._metadata_sink = metadata_sink

    def land(
        self,
        *,
        provider: str,
        dataset: str,
        logical_key: str,
        published_at: datetime,
        first_seen_at: datetime,
        payload: SensitivePayload,
        content_type: str,
        principal: str,
        purpose: str,
    ) -> LandingReceipt:
        published = self._utc(published_at, "published_at")
        first_seen = self._utc(first_seen_at, "first_seen_at")
        if published > first_seen:
            raise ValueError("published_at must be less than or equal to first_seen_at")
        values = (provider, dataset, logical_key, content_type, principal, purpose)
        if any(not value.strip() for value in values):
            raise ValueError("landing fields must not be blank")
        safe_component = re.compile(r"^[A-Za-z0-9._-]+$")
        if not safe_component.fullmatch(provider) or not safe_component.fullmatch(dataset):
            raise ValueError("provider and dataset must be safe object-key components")
        context = LandingContext(
            principal=principal,
            purpose=purpose,
            operation=LandingOperation.WRITE,
            provider=provider,
            dataset=dataset,
        )
        if not self._authorizer.authorize(context):
            raise LandingAccessDenied("raw landing access denied")

        plaintext = payload.bytes_for_authorized_processing()
        if not plaintext:
            raise ValueError("payload must not be empty")
        plaintext_hash = hashlib.sha256(plaintext).hexdigest()
        logical_key_hash = hashlib.sha256(logical_key.encode()).hexdigest()
        identity = "|".join(
            (provider, dataset, logical_key_hash, first_seen.isoformat(), plaintext_hash)
        ).encode()
        landing_id = hashlib.sha256(identity).hexdigest()
        object_key = f"licensed/{provider}/{dataset}/{landing_id}"
        associated_data = f"marketpilot:{landing_id}:{provider}:{dataset}".encode()
        encrypted = self._object_store.get(object_key)
        if encrypted is None:
            envelope = self._cipher.encrypt(plaintext, associated_data=associated_data)
            if not envelope.ciphertext or envelope.ciphertext == plaintext:
                raise ValueError("cipher must return non-plaintext ciphertext")
            if not envelope.key_id.strip() or not envelope.algorithm.strip() or not envelope.nonce:
                raise ValueError("cipher envelope is incomplete")
            candidate = EncryptedLandingObject(
                object_key=object_key,
                envelope=envelope,
                content_type=content_type,
                plaintext_sha256=plaintext_hash,
            )
            encrypted = self._object_store.put_if_absent(candidate)
        if (
            encrypted.object_key != object_key
            or encrypted.plaintext_sha256 != plaintext_hash
            or encrypted.content_type != content_type
        ):
            raise ValueError("immutable raw landing object conflict")
        envelope = encrypted.envelope
        receipt = LandingReceipt(
            landing_id=landing_id,
            object_key=object_key,
            provider=provider,
            dataset=dataset,
            logical_key_hash=logical_key_hash,
            published_at=published,
            first_seen_at=first_seen,
            plaintext_sha256=plaintext_hash,
            key_id=envelope.key_id,
            algorithm=envelope.algorithm,
            content_type=content_type,
        )
        self._metadata_sink.append(receipt)
        return receipt

    def read_authorized(
        self,
        receipt: LandingReceipt,
        *,
        principal: str,
        purpose: str,
    ) -> SensitivePayload:
        context = LandingContext(
            principal=principal,
            purpose=purpose,
            operation=LandingOperation.READ,
            provider=receipt.provider,
            dataset=receipt.dataset,
        )
        if not self._authorizer.authorize(context):
            raise LandingAccessDenied("raw landing access denied")
        stored = self._object_store.get(receipt.object_key)
        if stored is None:
            raise KeyError("encrypted landing object is unavailable")
        if (
            stored.object_key != receipt.object_key
            or stored.plaintext_sha256 != receipt.plaintext_sha256
            or stored.content_type != receipt.content_type
            or stored.envelope.key_id != receipt.key_id
            or stored.envelope.algorithm != receipt.algorithm
        ):
            raise ValueError("raw landing object metadata integrity check failed")
        associated_data = (
            f"marketpilot:{receipt.landing_id}:{receipt.provider}:{receipt.dataset}".encode()
        )
        plaintext = self._cipher.decrypt(stored.envelope, associated_data=associated_data)
        if hashlib.sha256(plaintext).hexdigest() != receipt.plaintext_sha256:
            raise ValueError("raw landing payload integrity check failed")
        return SensitivePayload(plaintext)

    @staticmethod
    def _utc(value: datetime, name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value.astimezone(UTC)
