from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from marketpilot.services.raw_landing import (
    EncryptedLandingObject,
    EncryptionEnvelope,
    LandingAccessDenied,
    LandingContext,
    LandingReceipt,
    LicensedPayloadLandingService,
    SensitivePayload,
)


class Authorizer:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.contexts: list[LandingContext] = []

    def authorize(self, context: LandingContext) -> bool:
        self.contexts.append(context)
        return self.allowed


class TestCipher:
    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> EncryptionEnvelope:
        mask = associated_data[0]
        return EncryptionEnvelope(
            ciphertext=bytes(value ^ mask for value in plaintext),
            key_id="kms/market-data/v7",
            algorithm="AES-256-GCM-test-double",
            nonce=b"test-nonce",
        )

    def decrypt(self, envelope: EncryptionEnvelope, *, associated_data: bytes) -> bytes:
        mask = associated_data[0]
        return bytes(value ^ mask for value in envelope.ciphertext)


class ObjectStore:
    def __init__(self) -> None:
        self.values: dict[str, EncryptedLandingObject] = {}

    def put_if_absent(self, value: EncryptedLandingObject) -> EncryptedLandingObject:
        existing = self.values.get(value.object_key)
        if existing is not None and existing != value:
            raise ValueError("immutable object conflict")
        self.values[value.object_key] = value
        return value

    def get(self, object_key: str) -> EncryptedLandingObject | None:
        return self.values.get(object_key)


class MetadataSink:
    def __init__(self) -> None:
        self.values: dict[str, LandingReceipt] = {}

    def append(self, receipt: LandingReceipt) -> None:
        existing = self.values.get(receipt.landing_id)
        if existing is not None and existing != receipt:
            raise ValueError("immutable metadata conflict")
        self.values[receipt.landing_id] = receipt


def service(allowed: bool = True) -> tuple[
    LicensedPayloadLandingService, Authorizer, ObjectStore, MetadataSink
]:
    authorizer = Authorizer(allowed)
    store = ObjectStore()
    sink = MetadataSink()
    return (
        LicensedPayloadLandingService(
            authorizer=authorizer,
            cipher=TestCipher(),
            object_store=store,
            metadata_sink=sink,
        ),
        authorizer,
        store,
        sink,
    )


def land(target: LicensedPayloadLandingService) -> LandingReceipt:
    return target.land(
        provider="licensed-feed",
        dataset="spxw-nbbo",
        logical_key="SPXW-20260817-6400-C@2026-08-17T13:30:00Z",
        published_at=datetime(2026, 8, 17, 13, 30, tzinfo=UTC),
        first_seen_at=datetime(2026, 8, 17, 13, 30, 1, tzinfo=UTC),
        payload=SensitivePayload(b'{"bid":12.25,"ask":12.40}'),
        content_type="application/json",
        principal="ingestor/webull-1",
        purpose="point-in-time-research",
    )


def test_authorized_payload_is_encrypted_idempotent_and_recoverable() -> None:
    target, authorizer, store, sink = service()

    first = land(target)
    second = land(target)

    assert first == second
    assert len(store.values) == 1
    assert len(sink.values) == 1
    encrypted = store.values[first.object_key]
    assert encrypted.envelope.ciphertext != b'{"bid":12.25,"ask":12.40}'
    recovered = target.read_authorized(
        first,
        principal="research/replay-1",
        purpose="point-in-time-research",
    )
    assert recovered.bytes_for_authorized_processing() == b'{"bid":12.25,"ask":12.40}'
    assert [context.operation for context in authorizer.contexts] == ["WRITE", "WRITE", "READ"]


def test_raw_bytes_are_absent_from_safe_metadata_and_representations() -> None:
    target, _, store, _ = service()
    secret = b'{"licensed":"do-not-log"}'
    receipt = target.land(
        provider="licensed-feed",
        dataset="events",
        logical_key="event/secret",
        published_at=datetime(2026, 8, 17, 12, tzinfo=UTC),
        first_seen_at=datetime(2026, 8, 17, 12, 0, 1, tzinfo=UTC),
        payload=SensitivePayload(secret),
        content_type="application/json",
        principal="ingestor/events-1",
        purpose="point-in-time-research",
    )

    metadata = str(asdict(receipt))
    encrypted = store.values[receipt.object_key]
    assert secret.decode() not in metadata
    assert secret.decode() not in repr(encrypted)
    assert secret.decode() not in repr(encrypted.envelope)
    assert secret.decode() not in repr(SensitivePayload(secret))
    assert "ciphertext=<redacted>" in repr(encrypted.envelope)


def test_denied_access_never_touches_cipher_or_store() -> None:
    target, authorizer, store, sink = service(allowed=False)

    with pytest.raises(LandingAccessDenied, match="raw landing access denied"):
        land(target)

    assert len(authorizer.contexts) == 1
    assert store.values == {}
    assert sink.values == {}


def test_landing_rejects_plaintext_cipher_and_invalid_point_in_time() -> None:
    class PlaintextCipher(TestCipher):
        def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> EncryptionEnvelope:
            return EncryptionEnvelope(plaintext, "key", "bad", b"nonce")

    authorizer = Authorizer(True)
    target = LicensedPayloadLandingService(
        authorizer=authorizer,
        cipher=PlaintextCipher(),
        object_store=ObjectStore(),
        metadata_sink=MetadataSink(),
    )
    with pytest.raises(ValueError, match="non-plaintext"):
        land(target)

    with pytest.raises(ValueError, match="published_at"):
        target.land(
            provider="licensed-feed",
            dataset="events",
            logical_key="event/1",
            published_at=datetime(2026, 8, 17, 12, 0, 2, tzinfo=UTC),
            first_seen_at=datetime(2026, 8, 17, 12, 0, 1, tzinfo=UTC),
            payload=SensitivePayload(b"secret"),
            content_type="application/octet-stream",
            principal="ingestor/events-1",
            purpose="research",
        )


def test_landing_rejects_unsafe_object_key_components() -> None:
    target, _, _, _ = service()

    with pytest.raises(ValueError, match="safe object-key"):
        target.land(
            provider="../licensed-feed",
            dataset="events",
            logical_key="event/1",
            published_at=datetime(2026, 8, 17, 12, tzinfo=UTC),
            first_seen_at=datetime(2026, 8, 17, 12, 0, 1, tzinfo=UTC),
            payload=SensitivePayload(b"secret"),
            content_type="application/octet-stream",
            principal="ingestor/events-1",
            purpose="research",
        )
