from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from marketpilot.domain.alert_delivery import AttemptOutcome, DeliveryChannel, OutboxEventKind
from marketpilot.services.alert_delivery_router import create_alert_delivery_router
from marketpilot.services.alert_delivery_service import (
    AlertDeliveryOrchestrator,
    AlertDeliveryPolicy,
    ChannelPolicy,
    DeliverySender,
    ScriptedDeliverySender,
    SenderResult,
)
from marketpilot.services.alert_delivery_store import AlertDeliveryConflict, AlertDeliveryStore

NOW = datetime(2026, 8, 18, 14, 0, tzinfo=UTC)
KEY = b"unit-test-signing-key"


def api_client() -> TestClient:
    app = FastAPI()
    app.include_router(create_alert_delivery_router())
    return TestClient(app)


def channel_policy(
    channel: DeliveryChannel = DeliveryChannel.WEBHOOK,
    *,
    max_attempts: int = 3,
    require_ack: bool = False,
    max_escalations: int = 0,
) -> ChannelPolicy:
    return ChannelPolicy(
        channel=channel,
        enabled=True,
        opted_in=True,
        authorization_id="local-test-authorization",
        cooldown=timedelta(minutes=5),
        max_attempts=max_attempts,
        base_backoff=timedelta(seconds=1),
        max_backoff=timedelta(seconds=4),
        require_ack=require_ack,
        ack_timeout=timedelta(seconds=1),
        max_escalations=max_escalations,
    )


def service(
    store: AlertDeliveryStore,
    *,
    policy: ChannelPolicy | None = None,
    sender: DeliverySender | None = None,
    network_authorized: bool = False,
) -> AlertDeliveryOrchestrator:
    selected = policy or channel_policy()
    return AlertDeliveryOrchestrator(
        store=store,
        policy=AlertDeliveryPolicy(version="test-v1", channels={selected.channel: selected}),
        signing_key=KEY,
        senders={selected.channel: sender} if sender is not None else None,
        network_authorized=network_authorized,
    )


def enqueue(
    orchestrator: AlertDeliveryOrchestrator,
    message_id: str,
    *,
    at: datetime = NOW,
    fingerprint: str = "risk-lock:spx",
    channel: DeliveryChannel = DeliveryChannel.WEBHOOK,
) -> None:
    orchestrator.enqueue(
        message_id=message_id,
        alert_id="alert-1",
        fingerprint=fingerprint,
        channel=channel,
        destination_reference="endpoint-ref:ops-primary",
        payload={"summary": "Risk Lock active", "action": "NO_TRADE"},
        created_at=at,
    )


def test_default_null_sender_is_disabled_and_never_claims_delivery() -> None:
    store = AlertDeliveryStore()
    orchestrator = service(store)
    enqueue(orchestrator, "message-null")

    attempts = orchestrator.dispatch_due(NOW)

    assert len(attempts) == 1
    assert attempts[0].sender_name == "null-disabled"
    assert attempts[0].outcome is AttemptOutcome.DISABLED
    assert [event.kind for event in store.events("message-null")] == [
        OutboxEventKind.ENQUEUED,
        OutboxEventKind.DELIVERY_DISABLED,
    ]
    assert orchestrator.dispatch_due(NOW + timedelta(minutes=1)) == ()
    assert orchestrator.verify_signature("message-null") is True
    stored = store.message("message-null")
    assert stored is not None
    assert stored.authorization_reference_hash is not None
    assert "local-test-authorization" not in stored.model_dump_json()

    missing_policy_store = AlertDeliveryStore()
    missing_policy_service = AlertDeliveryOrchestrator(
        store=missing_policy_store,
        policy=AlertDeliveryPolicy(version="disabled-default-v1", channels={}),
        signing_key=KEY,
    )
    enqueue(missing_policy_service, "message-policy-disabled")
    assert missing_policy_store.events("message-policy-disabled")[0].kind is (
        OutboxEventKind.DELIVERY_DISABLED
    )
    assert missing_policy_service.dispatch_due(NOW) == ()


def test_retry_backoff_recovers_and_records_every_attempt_append_only() -> None:
    store = AlertDeliveryStore()
    sender = ScriptedDeliverySender(
        DeliveryChannel.WEBHOOK,
        (
            SenderResult(AttemptOutcome.TRANSIENT_FAILURE, "TIMEOUT"),
            SenderResult(AttemptOutcome.TRANSIENT_FAILURE, "RATE_LIMIT"),
            SenderResult(AttemptOutcome.SENT, response_reference="scenario-receipt-1"),
        ),
    )
    orchestrator = service(store, sender=sender)
    enqueue(orchestrator, "message-retry")

    first = orchestrator.dispatch_due(NOW)
    assert first[0].outcome is AttemptOutcome.TRANSIENT_FAILURE
    assert orchestrator.dispatch_due(NOW + timedelta(milliseconds=999)) == ()
    second = orchestrator.dispatch_due(NOW + timedelta(seconds=1))
    assert second[0].attempt_number == 2
    assert orchestrator.dispatch_due(NOW + timedelta(seconds=2)) == ()
    third = orchestrator.dispatch_due(NOW + timedelta(seconds=3))

    assert third[0].outcome is AttemptOutcome.SENT
    assert [attempt.outcome for attempt in store.attempts("message-retry")] == [
        AttemptOutcome.TRANSIENT_FAILURE,
        AttemptOutcome.TRANSIENT_FAILURE,
        AttemptOutcome.SENT,
    ]
    assert [event.kind for event in store.events("message-retry")] == [
        OutboxEventKind.ENQUEUED,
        OutboxEventKind.RETRY_SCHEDULED,
        OutboxEventKind.RETRY_SCHEDULED,
        OutboxEventKind.DELIVERED,
    ]


def test_retry_budget_and_permanent_failure_dead_letter() -> None:
    transient_store = AlertDeliveryStore()
    transient = ScriptedDeliverySender(
        DeliveryChannel.WEBHOOK,
        (
            SenderResult(AttemptOutcome.TRANSIENT_FAILURE, "TIMEOUT"),
            SenderResult(AttemptOutcome.TRANSIENT_FAILURE, "TIMEOUT"),
        ),
    )
    transient_service = service(
        transient_store,
        policy=channel_policy(max_attempts=2),
        sender=transient,
    )
    enqueue(transient_service, "message-exhausted")
    transient_service.dispatch_due(NOW)
    transient_service.dispatch_due(NOW + timedelta(seconds=1))
    terminal = transient_store.events("message-exhausted")[-1]
    assert terminal.kind is OutboxEventKind.DEAD_LETTERED
    assert terminal.reason_code == "RETRY_BUDGET_EXHAUSTED"

    permanent_store = AlertDeliveryStore()
    permanent = ScriptedDeliverySender(
        DeliveryChannel.WEBHOOK,
        (SenderResult(AttemptOutcome.PERMANENT_FAILURE, "DESTINATION_REVOKED"),),
    )
    permanent_service = service(permanent_store, sender=permanent)
    enqueue(permanent_service, "message-permanent")
    permanent_service.dispatch_due(NOW)
    assert permanent_store.events("message-permanent")[-1].kind is OutboxEventKind.DEAD_LETTERED


def test_dedupe_cooldown_suppresses_new_message_but_is_idempotent_by_id() -> None:
    store = AlertDeliveryStore()
    orchestrator = service(store)
    enqueue(orchestrator, "message-1")
    enqueue(orchestrator, "message-1")
    enqueue(orchestrator, "message-2", at=NOW + timedelta(minutes=1))
    enqueue(orchestrator, "message-3", at=NOW + timedelta(minutes=7))

    assert [event.kind for event in store.events("message-1")] == [OutboxEventKind.ENQUEUED]
    assert store.events("message-2")[0].kind is OutboxEventKind.SUPPRESSED
    assert store.events("message-3")[0].kind is OutboxEventKind.ENQUEUED
    assert len(store.messages()) == 3


def test_acknowledgment_stops_escalation_and_timeout_eventually_dead_letters() -> None:
    store = AlertDeliveryStore()
    sender = ScriptedDeliverySender(
        DeliveryChannel.MOBILE,
        (
            SenderResult(AttemptOutcome.SENT),
            SenderResult(AttemptOutcome.SENT),
        ),
    )
    orchestrator = service(
        store,
        policy=channel_policy(
            DeliveryChannel.MOBILE,
            require_ack=True,
            max_escalations=1,
        ),
        sender=sender,
    )
    enqueue(orchestrator, "message-ack", channel=DeliveryChannel.MOBILE)
    orchestrator.dispatch_due(NOW)
    assert orchestrator.escalate_due(NOW + timedelta(milliseconds=999)) == ()
    escalated = orchestrator.escalate_due(NOW + timedelta(seconds=1))
    assert escalated[0].kind is OutboxEventKind.ESCALATED
    assert orchestrator.escalate_due(NOW + timedelta(seconds=1)) == ()
    orchestrator.dispatch_due(NOW + timedelta(seconds=1))
    acknowledged = orchestrator.acknowledge(
        "message-ack", actor="operator-1", acknowledged_at=NOW + timedelta(seconds=1.5)
    )
    assert acknowledged.kind is OutboxEventKind.ACKNOWLEDGED
    assert orchestrator.escalate_due(NOW + timedelta(seconds=5)) == ()
    assert store.events("message-ack")[-1].kind is OutboxEventKind.ACKNOWLEDGED

    dead_store = AlertDeliveryStore()
    dead_sender = ScriptedDeliverySender(
        DeliveryChannel.MOBILE,
        (SenderResult(AttemptOutcome.SENT), SenderResult(AttemptOutcome.SENT)),
    )
    dead_service = service(
        dead_store,
        policy=channel_policy(
            DeliveryChannel.MOBILE,
            require_ack=True,
            max_escalations=1,
        ),
        sender=dead_sender,
    )
    enqueue(dead_service, "message-unacked", channel=DeliveryChannel.MOBILE)
    dead_service.dispatch_due(NOW)
    dead_service.escalate_due(NOW + timedelta(seconds=1))
    dead_service.dispatch_due(NOW + timedelta(seconds=1))
    terminal = dead_service.escalate_due(NOW + timedelta(seconds=2))
    assert terminal[0].kind is OutboxEventKind.DEAD_LETTERED
    assert terminal[0].reason_code == "ACK_TIMEOUT"


class NetworkSpySender:
    name = "network-spy"
    channel = DeliveryChannel.WEBHOOK
    network_capable = True

    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: object) -> SenderResult:
        del message
        self.calls += 1
        return SenderResult(AttemptOutcome.SENT)


class RaisingSender:
    name = "raising-local"
    channel = DeliveryChannel.WEBHOOK
    network_capable = False

    def send(self, message: object) -> SenderResult:
        del message
        raise RuntimeError("secret transport internals must not be persisted")


def test_network_sender_is_not_called_without_dual_authorization_and_errors_are_sanitized() -> None:
    network_store = AlertDeliveryStore()
    spy = NetworkSpySender()
    network_service = service(network_store, sender=spy, network_authorized=False)
    enqueue(network_service, "message-network")
    attempt = network_service.dispatch_due(NOW)[0]
    assert spy.calls == 0
    assert attempt.outcome is AttemptOutcome.DISABLED
    assert attempt.reason_code == "NETWORK_NOT_AUTHORIZED"

    raising_store = AlertDeliveryStore()
    raising_service = service(raising_store, sender=RaisingSender())
    enqueue(raising_service, "message-raising")
    failed = raising_service.dispatch_due(NOW)[0]
    assert failed.outcome is AttemptOutcome.TRANSIENT_FAILURE
    assert failed.reason_code == "SENDER_EXCEPTION"
    assert "secret transport" not in failed.model_dump_json()


def test_store_survives_restart_and_sqlite_rejects_update_delete(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    first = AlertDeliveryStore(path)
    orchestrator = service(first)
    enqueue(orchestrator, "message-persisted")
    orchestrator.dispatch_due(NOW)
    first.close()

    restarted = AlertDeliveryStore(path)
    assert restarted.message("message-persisted") is not None
    with pytest.raises(sqlite3.IntegrityError, match="append-only table"):
        restarted._connection.execute(  # noqa: SLF001
            "UPDATE alert_outbox_messages SET channel = 'EMAIL' WHERE message_id = ?",
            ("message-persisted",),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only table"):
        restarted._connection.execute(  # noqa: SLF001
            "DELETE FROM alert_outbox_events WHERE message_id = ?", ("message-persisted",)
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only table"):
        restarted._connection.execute(  # noqa: SLF001
            "UPDATE alert_delivery_attempts SET outcome = 'SENT' WHERE message_id = ?",
            ("message-persisted",),
        )
    restarted.close()


def test_router_is_scenario_local_no_trade_and_rejects_urls_or_live_mode() -> None:
    base = {
        "run_mode": "SCENARIO",
        "scope": "LOCAL",
        "policy_version": "scenario-v1",
        "channels": [
            {
                "channel": "EMAIL",
                "enabled": True,
                "opted_in": True,
                "authorization_id": "local-fixture",
            }
        ],
        "sender_mode": "SCRIPTED",
        "sender_scripts": [
            {"channel": "EMAIL", "outcomes": [{"outcome": "SENT"}]}
        ],
        "operations": [
            {
                "kind": "ENQUEUE",
                "message_id": "message-api",
                "alert_id": "alert-api",
                "fingerprint": "risk-lock:api",
                "channel": "EMAIL",
                "destination_reference": "email-ref:operator-primary",
                "payload": {"action": "NO_TRADE"},
                "created_at": NOW.isoformat(),
            },
            {"kind": "DISPATCH", "as_of": NOW.isoformat()},
        ],
    }
    response = api_client().post("/v1/scenario/alert-delivery/run", json=base)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_mode"] == "SCENARIO"
    assert body["scope"] == "LOCAL"
    assert body["verification"] == "UNVERIFIED"
    assert body["execution_enabled"] is False
    assert body["action"] == "NO_TRADE"
    assert body["network_authorized"] is False
    assert body["attempts"][0]["outcome"] == "SENT"

    base["run_mode"] = "LIVE"
    assert api_client().post("/v1/scenario/alert-delivery/run", json=base).status_code == 422
    base["run_mode"] = "SCENARIO"
    operations = base["operations"]
    assert isinstance(operations, list)
    operations[0]["destination_reference"] = "https://attacker.invalid/ssrf"
    rejected = api_client().post("/v1/scenario/alert-delivery/run", json=base)
    assert rejected.status_code == 422


def test_immutable_identity_conflicts_are_rejected() -> None:
    store = AlertDeliveryStore()
    orchestrator = service(store)
    enqueue(orchestrator, "message-conflict")
    with pytest.raises(AlertDeliveryConflict, match="immutable audit conflict"):
        orchestrator.enqueue(
            message_id="message-conflict",
            alert_id="different-alert",
            fingerprint="different",
            channel=DeliveryChannel.WEBHOOK,
            destination_reference="endpoint-ref:ops-primary",
            payload={"different": True},
            created_at=NOW,
        )
