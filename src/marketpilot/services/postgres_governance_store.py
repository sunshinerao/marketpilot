from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from marketpilot.domain.governance import (
    ApprovalAction,
    GovernanceApproval,
    GovernanceError,
    GovernanceEvent,
    ModelVersion,
)
from marketpilot.services.persistence_contracts import (
    ConnectionFactory,
    GovernanceSessionFreeze,
)
from marketpilot.services.repository import ImmutableAuditConflict


class PostgreSQLGovernanceStore:
    """Append-only governance state with transactional approval/event recording."""

    _TABLES = (
        "governance_model_versions",
        "governance_approvals",
        "governance_events",
        "governance_session_freezes",
    )

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection = connection_factory()
        self._lock = RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def append_model_version(self, model: ModelVersion) -> None:
        payload = self._model_payload(model)
        serialized = self._serialize(payload)
        with self._lock:
            cursor = self._connection.cursor()
            try:
                if model.parent_version is not None:
                    parent = self._model_row(cursor, model.model_id, model.parent_version)
                    if parent is None:
                        raise GovernanceError(f"unknown parent version: {model.parent_version}")
                cursor.execute(
                    """
                    INSERT INTO marketpilot.governance_model_versions(
                        model_id, version, parent_version, trained_at, payload_json
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (model_id, version) DO NOTHING
                    """,
                    (
                        model.model_id,
                        model.version,
                        model.parent_version,
                        model.trained_at.isoformat(),
                        serialized,
                    ),
                )
                if cursor.rowcount != 1:
                    row = self._model_row(cursor, model.model_id, model.version, lock=True)
                    existing = None if row is None else self._payload(row, 0)
                    if existing != payload:
                        raise ImmutableAuditConflict(
                            f"immutable governance model conflict: "
                            f"{model.model_id}@{model.version}"
                        )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    def model_versions(self, model_id: str) -> tuple[ModelVersion, ...]:
        rows = self._read_all(
            """
            SELECT payload_json
            FROM marketpilot.governance_model_versions
            WHERE model_id = %s ORDER BY trained_at, version
            """,
            (model_id,),
        )
        return tuple(self._model(self._payload(row, 0)) for row in rows)

    def model_version(self, model_id: str, version: str) -> ModelVersion | None:
        row = self._read_one(
            """
            SELECT payload_json FROM marketpilot.governance_model_versions
            WHERE model_id = %s AND version = %s
            """,
            (model_id, version),
        )
        return None if row is None else self._model(self._payload(row, 0))

    def append_action(
        self,
        approval: GovernanceApproval,
        event: GovernanceEvent,
    ) -> None:
        approval.verify()
        self._validate_event_matches_approval(approval, event)
        approval_payload = self._approval_payload(approval)
        event_payload = self._event_payload(event)
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (event.model_id,),
                )
                already_recorded = self._event_by_approval_row(
                    cursor, approval.approval_id, lock=True
                )
                if already_recorded is not None:
                    existing_event = self._payload(already_recorded, 0)
                    existing_approval_row = self._approval_row(
                        cursor, approval.approval_id, lock=True
                    )
                    existing_approval = (
                        None
                        if existing_approval_row is None
                        else self._payload(existing_approval_row, 0)
                    )
                    if existing_event != event_payload or existing_approval != approval_payload:
                        raise ImmutableAuditConflict(
                            f"immutable governance action conflict: {approval.approval_id}"
                        )
                    raise GovernanceError("governance approval has already been used")

                current = self._current_champion_row(cursor, event.model_id, lock=True)
                current_version = (
                    None if current is None else str(self._cell(current, "target_version", 0))
                )
                if event.source_version != current_version:
                    raise GovernanceError("approval source is not the current champion")
                target_row = self._model_row(
                    cursor, event.model_id, event.target_version, lock=True
                )
                if target_row is None:
                    raise GovernanceError(
                        f"unknown model version: {event.model_id}@{event.target_version}"
                    )
                target = self._model(self._payload(target_row, 0))
                self._validate_action(approval, target, current_version)

                self._insert_approval(cursor, approval, approval_payload)
                cursor.execute(
                    """
                    INSERT INTO marketpilot.governance_events(
                        approval_id, model_id, source_version, target_version,
                        occurred_at, payload_json
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (approval_id) DO NOTHING
                    """,
                    (
                        event.approval_id,
                        event.model_id,
                        event.source_version,
                        event.target_version,
                        event.occurred_at.isoformat(),
                        self._serialize(event_payload),
                    ),
                )
                if cursor.rowcount != 1:
                    raise GovernanceError("governance approval has already been used")
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    def current_champion(self, model_id: str) -> ModelVersion | None:
        row = self._read_one(
            """
            SELECT target_version FROM marketpilot.governance_events
            WHERE model_id = %s ORDER BY event_id DESC LIMIT 1
            """,
            (model_id,),
        )
        if row is None:
            return None
        return self.model_version(model_id, str(self._cell(row, "target_version", 0)))

    def events(self) -> tuple[GovernanceEvent, ...]:
        rows = self._read_all(
            "SELECT payload_json FROM marketpilot.governance_events ORDER BY event_id"
        )
        return tuple(self._event(self._payload(row, 0)) for row in rows)

    def approvals(self) -> tuple[GovernanceApproval, ...]:
        rows = self._read_all(
            """
            SELECT payload_json FROM marketpilot.governance_approvals
            ORDER BY approved_at, approval_id
            """
        )
        return tuple(self._approval(self._payload(row, 0)) for row in rows)

    def freeze_session(
        self,
        model_id: str,
        session_id: str,
        *,
        frozen_at: datetime,
    ) -> GovernanceSessionFreeze:
        if not session_id.strip():
            raise GovernanceError("session_id must not be blank")
        timestamp = self._utc(frozen_at, "frozen_at")
        lock_key = f"{model_id}\x1f{session_id}"
        with self._lock:
            cursor = self._connection.cursor()
            try:
                # Serialize against model promotion first, then against another freezer
                # for the same session. This gives every freeze a champion state that
                # corresponds to a committed governance order.
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (model_id,)
                )
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,)
                )
                existing = self._session_row(cursor, model_id, session_id, lock=True)
                if existing is not None:
                    result = self._freeze(self._payload(existing, 0))
                    self._connection.commit()
                    return result
                champion_row = self._current_champion_row(cursor, model_id, lock=True)
                if champion_row is None:
                    raise GovernanceError(f"model has no champion: {model_id}")
                version = str(self._cell(champion_row, "target_version", 0))
                freeze = GovernanceSessionFreeze(model_id, session_id, version, timestamp)
                payload = self._freeze_payload(freeze)
                cursor.execute(
                    """
                    INSERT INTO marketpilot.governance_session_freezes(
                        model_id, session_id, version, frozen_at, payload_json
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (model_id, session_id) DO NOTHING
                    """,
                    (
                        model_id,
                        session_id,
                        version,
                        timestamp.isoformat(),
                        self._serialize(payload),
                    ),
                )
                if cursor.rowcount != 1:
                    winner = self._session_row(cursor, model_id, session_id, lock=True)
                    if winner is None:
                        raise RuntimeError("session freeze insert was not readable")
                    freeze = self._freeze(self._payload(winner, 0))
                self._connection.commit()
                return freeze
            except Exception:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    def session_freeze(
        self, model_id: str, session_id: str
    ) -> GovernanceSessionFreeze | None:
        row = self._read_one(
            """
            SELECT payload_json FROM marketpilot.governance_session_freezes
            WHERE model_id = %s AND session_id = %s
            """,
            (model_id, session_id),
        )
        return None if row is None else self._freeze(self._payload(row, 0))

    def verify_append_only_triggers(self) -> bool:
        row = self._read_one(
            """
            SELECT count(*)
            FROM pg_trigger t
            JOIN pg_class r ON r.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = r.relnamespace
            JOIN pg_proc p ON p.oid = t.tgfoid
            WHERE n.nspname = %s
              AND r.relname = ANY(%s)
              AND p.proname = %s
              AND NOT t.tgisinternal
              AND t.tgenabled <> 'D'
            """,
            ("marketpilot", list(self._TABLES), "deny_audit_mutation"),
        )
        return row is not None and int(self._cell(row, "count", 0)) == 8

    def _insert_approval(
        self,
        cursor: Any,
        approval: GovernanceApproval,
        payload: dict[str, Any],
    ) -> None:
        cursor.execute(
            """
            INSERT INTO marketpilot.governance_approvals(
                approval_id, model_id, approved_at, payload_json
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (approval_id) DO NOTHING
            """,
            (
                approval.approval_id,
                approval.model_id,
                approval.approved_at.isoformat(),
                self._serialize(payload),
            ),
        )
        if cursor.rowcount == 1:
            return
        row = self._approval_row(cursor, approval.approval_id, lock=True)
        if row is None or self._payload(row, 0) != payload:
            raise ImmutableAuditConflict(
                f"immutable governance approval conflict: {approval.approval_id}"
            )

    @staticmethod
    def _validate_event_matches_approval(
        approval: GovernanceApproval, event: GovernanceEvent
    ) -> None:
        expected = (
            approval.action,
            approval.model_id,
            approval.source_version,
            approval.target_version,
            approval.approval_id,
            approval.approved_at,
        )
        actual = (
            event.action,
            event.model_id,
            event.source_version,
            event.target_version,
            event.approval_id,
            event.occurred_at,
        )
        if actual != expected:
            raise GovernanceError("governance event does not match approval")

    @staticmethod
    def _validate_action(
        approval: GovernanceApproval,
        target: ModelVersion,
        current_version: str | None,
    ) -> None:
        if approval.action is ApprovalAction.PROMOTE:
            if target.validation_report_hash is None:
                raise GovernanceError("challenger has no frozen validation report")
            if approval.evidence_hash != target.validation_report_hash:
                raise GovernanceError("promotion evidence does not match validation report")
            if approval.approved_at < target.trained_at:
                raise GovernanceError("promotion approval predates the challenger")
            if current_version is not None and target.parent_version != current_version:
                raise GovernanceError(
                    "challenger lineage must descend from the current champion"
                )
        elif target.version == current_version:
            raise GovernanceError("rollback target is already the champion")

    def _model_row(
        self, cursor: Any, model_id: str, version: str, *, lock: bool = False
    ) -> Any | None:
        suffix = " FOR SHARE" if lock else ""
        cursor.execute(
            "SELECT payload_json FROM marketpilot.governance_model_versions "
            f"WHERE model_id = %s AND version = %s{suffix}",
            (model_id, version),
        )
        return cursor.fetchone()

    def _approval_row(self, cursor: Any, approval_id: str, *, lock: bool) -> Any | None:
        suffix = " FOR SHARE" if lock else ""
        cursor.execute(
            "SELECT payload_json FROM marketpilot.governance_approvals "
            f"WHERE approval_id = %s{suffix}",
            (approval_id,),
        )
        return cursor.fetchone()

    def _event_by_approval_row(
        self, cursor: Any, approval_id: str, *, lock: bool
    ) -> Any | None:
        suffix = " FOR SHARE" if lock else ""
        cursor.execute(
            "SELECT payload_json FROM marketpilot.governance_events "
            f"WHERE approval_id = %s{suffix}",
            (approval_id,),
        )
        return cursor.fetchone()

    def _current_champion_row(self, cursor: Any, model_id: str, *, lock: bool) -> Any | None:
        suffix = " FOR SHARE" if lock else ""
        cursor.execute(
            "SELECT target_version FROM marketpilot.governance_events "
            f"WHERE model_id = %s ORDER BY event_id DESC LIMIT 1{suffix}",
            (model_id,),
        )
        return cursor.fetchone()

    def _session_row(
        self, cursor: Any, model_id: str, session_id: str, *, lock: bool
    ) -> Any | None:
        suffix = " FOR SHARE" if lock else ""
        cursor.execute(
            "SELECT payload_json FROM marketpilot.governance_session_freezes "
            f"WHERE model_id = %s AND session_id = %s{suffix}",
            (model_id, session_id),
        )
        return cursor.fetchone()

    def _read_one(self, query: str, params: tuple[Any, ...] = ()) -> Any | None:
        rows = self._read_all(query, params)
        return rows[0] if rows else None

    def _read_all(self, query: str, params: tuple[Any, ...] = ()) -> list[Any]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(query, params)
                rows = cursor.fetchall()
                self._connection.commit()
                return rows
            except Exception:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    @staticmethod
    def _model_payload(model: ModelVersion) -> dict[str, Any]:
        payload = asdict(model)
        payload["trained_at"] = model.trained_at.isoformat()
        return payload

    @staticmethod
    def _approval_payload(approval: GovernanceApproval) -> dict[str, Any]:
        payload = asdict(approval)
        payload["action"] = approval.action.value
        payload["approved_at"] = approval.approved_at.isoformat()
        return payload

    @staticmethod
    def _event_payload(event: GovernanceEvent) -> dict[str, Any]:
        payload = asdict(event)
        payload["action"] = event.action.value
        payload["occurred_at"] = event.occurred_at.isoformat()
        return payload

    @staticmethod
    def _freeze_payload(freeze: GovernanceSessionFreeze) -> dict[str, Any]:
        payload = asdict(freeze)
        payload["frozen_at"] = freeze.frozen_at.isoformat()
        return payload

    @staticmethod
    def _model(payload: dict[str, Any]) -> ModelVersion:
        return ModelVersion(
            model_id=str(payload["model_id"]),
            version=str(payload["version"]),
            artifact_hash=str(payload["artifact_hash"]),
            data_manifest_hash=str(payload["data_manifest_hash"]),
            trained_at=PostgreSQLGovernanceStore._datetime(payload["trained_at"]),
            validation_report_hash=PostgreSQLGovernanceStore._optional(
                payload["validation_report_hash"]
            ),
            parent_version=PostgreSQLGovernanceStore._optional(payload["parent_version"]),
        )

    @staticmethod
    def _approval(payload: dict[str, Any]) -> GovernanceApproval:
        return GovernanceApproval(
            approval_id=str(payload["approval_id"]),
            action=ApprovalAction(str(payload["action"])),
            model_id=str(payload["model_id"]),
            source_version=PostgreSQLGovernanceStore._optional(payload["source_version"]),
            target_version=str(payload["target_version"]),
            approved_by=str(payload["approved_by"]),
            approved_at=PostgreSQLGovernanceStore._datetime(payload["approved_at"]),
            evidence_hash=str(payload["evidence_hash"]),
            note=str(payload["note"]),
        )

    @staticmethod
    def _event(payload: dict[str, Any]) -> GovernanceEvent:
        return GovernanceEvent(
            action=ApprovalAction(str(payload["action"])),
            model_id=str(payload["model_id"]),
            source_version=PostgreSQLGovernanceStore._optional(payload["source_version"]),
            target_version=str(payload["target_version"]),
            approval_id=str(payload["approval_id"]),
            occurred_at=PostgreSQLGovernanceStore._datetime(payload["occurred_at"]),
        )

    @staticmethod
    def _freeze(payload: dict[str, Any]) -> GovernanceSessionFreeze:
        return GovernanceSessionFreeze(
            model_id=str(payload["model_id"]),
            session_id=str(payload["session_id"]),
            version=str(payload["version"]),
            frozen_at=PostgreSQLGovernanceStore._datetime(payload["frozen_at"]),
        )

    @staticmethod
    def _payload(row: Any, index: int) -> dict[str, Any]:
        return PostgreSQLGovernanceStore._object(
            PostgreSQLGovernanceStore._cell(row, "payload_json", index)
        )

    @staticmethod
    def _serialize(value: dict[str, Any]) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _object(value: Any) -> dict[str, Any]:
        decoded = json.loads(value) if isinstance(value, str) else value
        if not isinstance(decoded, dict):
            raise TypeError("governance payload must be a JSON object")
        return decoded

    @staticmethod
    def _cell(row: Any, name: str, index: int) -> Any:
        if isinstance(row, dict):
            return row[name]
        try:
            return row[name]
        except (TypeError, KeyError):
            return row[index]

    @staticmethod
    def _datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    @staticmethod
    def _optional(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _utc(value: datetime, name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise GovernanceError(f"{name} must be timezone-aware")
        return value.astimezone(UTC)


class PostgreSQLFrozenChampionRegistry:
    """Drop-in durable counterpart to the in-process ``FrozenChampionRegistry``."""

    def __init__(
        self,
        store: PostgreSQLGovernanceStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def register_challenger(self, model: ModelVersion) -> None:
        self._store.append_model_version(model)

    def promote(
        self, model_id: str, version: str, approval: GovernanceApproval
    ) -> ModelVersion:
        event = GovernanceEvent(
            action=ApprovalAction.PROMOTE,
            model_id=model_id,
            source_version=approval.source_version,
            target_version=version,
            approval_id=approval.approval_id,
            occurred_at=approval.approved_at,
        )
        self._store.append_action(approval, event)
        return self._get(model_id, version)

    def rollback(
        self,
        model_id: str,
        target_version: str,
        approval: GovernanceApproval,
    ) -> ModelVersion:
        event = GovernanceEvent(
            action=ApprovalAction.ROLLBACK,
            model_id=model_id,
            source_version=approval.source_version,
            target_version=target_version,
            approval_id=approval.approval_id,
            occurred_at=approval.approved_at,
        )
        self._store.append_action(approval, event)
        return self._get(model_id, target_version)

    def freeze_session(self, model_id: str, session_id: str) -> ModelVersion:
        freeze = self._store.freeze_session(
            model_id, session_id, frozen_at=self._clock()
        )
        return self._get(model_id, freeze.version)

    def champion(self, model_id: str, *, session_id: str | None = None) -> ModelVersion:
        if session_id is not None:
            freeze = self._store.session_freeze(model_id, session_id)
            if freeze is not None:
                return self._get(model_id, freeze.version)
        champion = self._store.current_champion(model_id)
        if champion is None:
            raise GovernanceError(f"model has no champion: {model_id}")
        return champion

    def lineage(self, model_id: str, version: str) -> tuple[ModelVersion, ...]:
        lineage: list[ModelVersion] = []
        seen: set[str] = set()
        current = self._get(model_id, version)
        while True:
            if current.version in seen:
                raise GovernanceError("model lineage contains a cycle")
            seen.add(current.version)
            lineage.append(current)
            if current.parent_version is None:
                return tuple(lineage)
            current = self._get(model_id, current.parent_version)

    def audit_events(self) -> tuple[GovernanceEvent, ...]:
        return self._store.events()

    def versions(self, model_id: str) -> tuple[ModelVersion, ...]:
        return self._store.model_versions(model_id)

    def _get(self, model_id: str, version: str) -> ModelVersion:
        model = self._store.model_version(model_id, version)
        if model is None:
            raise GovernanceError(f"unknown model version: {model_id}@{version}")
        return model
