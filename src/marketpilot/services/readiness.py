from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from marketpilot.domain.readiness import (
    REQUIRED_EXTERNAL_EVIDENCE,
    EvidenceStatus,
    ReadinessGateReport,
    ReadinessManifest,
    ShadowEvidenceReport,
    ShadowLedgerEntry,
    ShadowSession,
)


class ReadinessEvidenceError(ValueError):
    pass


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _entry_hash(sequence: int, previous_hash: str, session: ShadowSession) -> str:
    payload = {
        "sequence": sequence,
        "previous_hash": previous_hash,
        "session": session.model_dump(mode="json"),
    }
    return f"sha256:{hashlib.sha256(_canonical_json(payload).encode()).hexdigest()}"


def load_readiness_manifest(path: str | Path) -> ReadinessManifest:
    source = Path(path)
    try:
        if source.is_symlink() or not source.is_file():
            raise ReadinessEvidenceError("readiness manifest must be a regular file")
        if source.stat().st_mode & 0o077:
            raise ReadinessEvidenceError("readiness manifest permissions are too broad")
        return ReadinessManifest.model_validate_json(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as exc:
        raise ReadinessEvidenceError("readiness manifest is missing or invalid") from exc


def save_readiness_manifest(path: str | Path, manifest: ReadinessManifest) -> None:
    destination = Path(path)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ReadinessEvidenceError("readiness manifest destination must be a regular file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(destination)


class ShadowLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> tuple[ShadowLedgerEntry, ...]:
        if not self.path.exists():
            return ()
        try:
            if self.path.is_symlink() or not self.path.is_file():
                raise ReadinessEvidenceError("shadow ledger must be a regular file")
            if self.path.stat().st_mode & 0o077:
                raise ReadinessEvidenceError("shadow ledger permissions are too broad")
            lines = self.path.read_text(encoding="utf-8").splitlines()
            entries = tuple(
                ShadowLedgerEntry.model_validate_json(line) for line in lines if line.strip()
            )
        except (OSError, UnicodeError, ValidationError) as exc:
            raise ReadinessEvidenceError("shadow ledger is unreadable or invalid") from exc
        self._verify(entries)
        return entries

    def append(self, session: ShadowSession) -> ShadowLedgerEntry:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ReadinessEvidenceError("shadow ledger must be a regular file")
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "r+", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                os.fchmod(stream.fileno(), 0o600)
                stream.seek(0)
                try:
                    entries = tuple(
                        ShadowLedgerEntry.model_validate_json(line)
                        for line in stream.read().splitlines()
                        if line.strip()
                    )
                except ValidationError as exc:
                    raise ReadinessEvidenceError(
                        "shadow ledger is invalid; append refused"
                    ) from exc
                self._verify(entries)
                if any(item.session.session_id == session.session_id for item in entries):
                    raise ReadinessEvidenceError("shadow session_id already exists")
                sequence = len(entries) + 1
                previous_hash = entries[-1].entry_hash if entries else "GENESIS"
                entry = ShadowLedgerEntry(
                    sequence=sequence,
                    previous_hash=previous_hash,
                    session=session,
                    entry_hash=_entry_hash(sequence, previous_hash, session),
                )
                stream.seek(0, os.SEEK_END)
                stream.write(entry.model_dump_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                return entry
        except Exception:
            # fdopen owns and closes the descriptor on both success and failure.
            raise

    @staticmethod
    def _verify(entries: tuple[ShadowLedgerEntry, ...]) -> None:
        previous_hash = "GENESIS"
        session_ids: set[str] = set()
        for expected_sequence, entry in enumerate(entries, start=1):
            if entry.sequence != expected_sequence or entry.previous_hash != previous_hash:
                raise ReadinessEvidenceError("shadow ledger chain is discontinuous")
            expected_hash = _entry_hash(entry.sequence, entry.previous_hash, entry.session)
            if entry.entry_hash != expected_hash:
                raise ReadinessEvidenceError("shadow ledger hash verification failed")
            if entry.session.session_id in session_ids:
                raise ReadinessEvidenceError("shadow ledger contains a duplicate session_id")
            session_ids.add(entry.session.session_id)
            previous_hash = entry.entry_hash


def evaluate_shadow_evidence(
    entries: tuple[ShadowLedgerEntry, ...],
    *,
    manifest_sha256: str,
    manifest_generated_at: datetime,
    evaluated_at: datetime,
    expected_code_version: str | None,
    minimum_sessions: int,
    minimum_trading_dates: int,
) -> ShadowEvidenceReport:
    if minimum_sessions <= 0 or minimum_trading_dates <= 0:
        raise ValueError("shadow evidence thresholds must be positive")
    if manifest_generated_at.tzinfo is None or manifest_generated_at.utcoffset() is None:
        raise ValueError("manifest_generated_at must be timezone-aware")
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    manifest_generated_at = manifest_generated_at.astimezone(UTC)
    evaluated_at = evaluated_at.astimezone(UTC)
    matching_manifest = [
        item.session
        for item in entries
        if item.session.readiness_manifest_sha256 == manifest_sha256
    ]
    invalid_time_window = [
        session
        for session in matching_manifest
        if session.started_at < manifest_generated_at or session.ended_at > evaluated_at
    ]
    mismatched_code_version = [
        session
        for session in matching_manifest
        if expected_code_version is not None
        and session.code_version != expected_code_version
    ]
    qualifying = [
        session
        for session in matching_manifest
        if session.started_at >= manifest_generated_at
        and session.ended_at <= evaluated_at
        and (
            expected_code_version is None
            or session.code_version == expected_code_version
        )
        and session.audit_integrity_passed
        and session.exact_es_contract is not None
        and bool(session.capability_report_ids)
        and session.decision_count > 0
    ]
    dates = {item.trading_date for item in qualifying}
    degradation = any(item.source_degradation_drill_passed for item in qualifying)
    recovery = any(item.recovery_drill_passed for item in qualifying)
    blockers: list[str] = []
    if invalid_time_window:
        blockers.append("SESSION_TIME_WINDOW")
    if mismatched_code_version:
        blockers.append("CODE_VERSION_MISMATCH")
    if len(qualifying) < minimum_sessions:
        blockers.append("SHADOW_SESSION_COUNT")
    if len(dates) < minimum_trading_dates:
        blockers.append("SHADOW_TRADING_DATE_COUNT")
    if not degradation:
        blockers.append("SOURCE_DEGRADATION_DRILL")
    if not recovery:
        blockers.append("RECOVERY_DRILL")
    return ShadowEvidenceReport(
        chain_valid=True,
        ledger_head_sha256=entries[-1].entry_hash if entries else None,
        recorded_sessions=len(entries),
        qualifying_sessions=len(qualifying),
        qualifying_trading_dates=len(dates),
        rejected_time_window_sessions=len(invalid_time_window),
        degradation_drill_observed=degradation,
        recovery_drill_observed=recovery,
        blockers=tuple(blockers),
    )


def evaluate_readiness(
    manifest: ReadinessManifest,
    entries: tuple[ShadowLedgerEntry, ...],
    *,
    evaluated_at: datetime,
    expected_code_version: str | None = None,
    minimum_sessions: int = 5,
    minimum_trading_dates: int = 3,
) -> ReadinessGateReport:
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    evaluated_at = evaluated_at.astimezone(UTC)
    by_requirement = {item.requirement: item for item in manifest.evidence}
    blockers: list[str] = []
    if manifest.generated_at > evaluated_at:
        blockers.append("MANIFEST_GENERATED_IN_FUTURE")
    for requirement in REQUIRED_EXTERNAL_EVIDENCE:
        evidence = by_requirement.get(requirement)
        if evidence is None:
            blockers.append(f"EVIDENCE_MISSING:{requirement.value}")
        elif evidence.status is not EvidenceStatus.VERIFIED:
            blockers.append(f"EVIDENCE_{evidence.status.value}:{requirement.value}")
        elif evidence.expires_at is None or evidence.expires_at <= evaluated_at:
            blockers.append(f"EVIDENCE_EXPIRED:{requirement.value}")
    manifest_hash = manifest.digest()
    shadow = evaluate_shadow_evidence(
        entries,
        manifest_sha256=manifest_hash,
        manifest_generated_at=manifest.generated_at,
        evaluated_at=evaluated_at,
        expected_code_version=expected_code_version,
        minimum_sessions=minimum_sessions,
        minimum_trading_dates=minimum_trading_dates,
    )
    blockers.extend(f"SHADOW:{item}" for item in shadow.blockers)
    return ReadinessGateReport(
        evaluated_at=evaluated_at,
        manifest_sha256=manifest_hash,
        evidence_complete=not any(
            item.startswith(("EVIDENCE_", "MANIFEST_")) for item in blockers
        ),
        shadow_evidence=shadow,
        shadow_admission_ready=not blockers,
        blockers=tuple(blockers),
    )
