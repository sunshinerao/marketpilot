from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from marketpilot.cli import main
from marketpilot.domain.readiness import (
    REQUIRED_EXTERNAL_EVIDENCE,
    EvidenceAuthority,
    EvidenceStatus,
    ReadinessEvidence,
    ReadinessManifest,
    ShadowSession,
)
from marketpilot.services.readiness import (
    ReadinessEvidenceError,
    ShadowLedger,
    evaluate_readiness,
    load_readiness_manifest,
    save_readiness_manifest,
)

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


def verified_manifest() -> ReadinessManifest:
    authorities = {
        "WEBULL_ACCOUNT_ENTITLEMENT": EvidenceAuthority.AUTHORIZED_PROVIDER,
        "LICENSED_MARKET_DATA_COVERAGE": EvidenceAuthority.INDEPENDENT_LICENSEE,
        "POINT_IN_TIME_TIMESTAMP_SEMANTICS": EvidenceAuthority.INDEPENDENT_LICENSEE,
        "EXPIRED_SPXW_NBBO_HISTORY": EvidenceAuthority.INDEPENDENT_LICENSEE,
        "LIVE_EVENT_SOURCE_COVERAGE": EvidenceAuthority.INDEPENDENT_LICENSEE,
        "PRODUCTION_SECURITY_AND_RECOVERY": EvidenceAuthority.INFRASTRUCTURE_CONTROL,
        "UNTOUCHED_HOLDOUT_APPROVAL": EvidenceAuthority.APPROVAL_AUTHORITY,
    }
    evidence = tuple(
        ReadinessEvidence(
            requirement=requirement,
            status=EvidenceStatus.VERIFIED,
            authority=authorities[requirement.value],
            issuer="redacted-authorized-reviewer",
            observed_at=NOW - timedelta(days=12),
            expires_at=NOW + timedelta(days=30),
            artifact_sha256=f"sha256:{'a' * 64}",
            review_id=f"review-{requirement.value.lower()}",
            scope="Redacted scope metadata; no account ID or licensed payload.",
            redactions_confirmed=True,
        )
        for requirement in REQUIRED_EXTERNAL_EVIDENCE
    )
    return ReadinessManifest(
        generated_at=NOW - timedelta(days=10),
        environment="production",
        evidence=evidence,
    )


def session(
    manifest: ReadinessManifest,
    sequence: int,
    *,
    degradation: bool = False,
    recovery: bool = False,
) -> ShadowSession:
    start = NOW - timedelta(days=3 - sequence)
    return ShadowSession(
        session_id=f"authorized-shadow-{sequence}",
        trading_date=start.astimezone(ZoneInfo("America/New_York")).date(),
        started_at=start,
        ended_at=start + timedelta(hours=7),
        environment="production-read-only",
        readiness_manifest_sha256=manifest.digest(),
        capability_report_ids=(f"webull-redacted-{sequence}", "licensed-feed-redacted"),
        exact_es_contract="ESU6",
        code_version="commit:abc123",
        rules_version="rules-v1",
        model_version="0.1.0-baseline",
        audit_export_sha256=f"sha256:{str(sequence % 10) * 64}",
        operator_review_id=f"operator-review-{sequence}",
        decision_count=3,
        no_trade_count=2,
        wait_count=1,
        audit_integrity_passed=True,
        source_degradation_drill_passed=degradation,
        recovery_drill_passed=recovery,
        source_degradation_evidence_sha256=(
            f"sha256:{'d' * 64}" if degradation else None
        ),
        recovery_evidence_sha256=f"sha256:{'e' * 64}" if recovery else None,
    )


def test_unverified_template_is_fail_closed_without_credentials() -> None:
    manifest = ReadinessManifest.unverified_template(
        generated_at=NOW,
        environment="production",
    )
    report = evaluate_readiness(
        manifest,
        (),
        evaluated_at=NOW,
        minimum_sessions=2,
        minimum_trading_dates=2,
    )

    assert report.evidence_complete is False
    assert report.shadow_admission_ready is False
    assert report.production_ready is False
    assert report.execution_enabled is False
    assert report.action == "NO_TRADE"
    assert "EVIDENCE_UNVERIFIED:WEBULL_ACCOUNT_ENTITLEMENT" in report.blockers


def test_local_simulation_cannot_be_declared_verified() -> None:
    with pytest.raises(ValidationError, match="LOCAL_SIMULATION"):
        ReadinessEvidence(
            requirement=REQUIRED_EXTERNAL_EVIDENCE[0],
            status=EvidenceStatus.VERIFIED,
            authority=EvidenceAuthority.LOCAL_SIMULATION,
            issuer="local",
            observed_at=NOW,
            expires_at=NOW + timedelta(days=1),
            artifact_sha256=f"sha256:{'b' * 64}",
            review_id="local-review",
            scope="simulation",
            redactions_confirmed=True,
        )


def test_requirement_rejects_wrong_external_authority() -> None:
    with pytest.raises(ValidationError, match="cannot verify WEBULL_ACCOUNT_ENTITLEMENT"):
        ReadinessEvidence(
            requirement=REQUIRED_EXTERNAL_EVIDENCE[0],
            status=EvidenceStatus.VERIFIED,
            authority=EvidenceAuthority.INDEPENDENT_LICENSEE,
            issuer="unrelated-licensee",
            observed_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=1),
            artifact_sha256=f"sha256:{'c' * 64}",
            review_id="wrong-authority",
            scope="Not a Webull entitlement review.",
            redactions_confirmed=True,
        )


def test_shadow_ledger_is_hash_chained_and_tamper_evident(tmp_path: Path) -> None:
    manifest = verified_manifest()
    path = tmp_path / "shadow.jsonl"
    ledger = ShadowLedger(path)
    first = ledger.append(session(manifest, 0))
    path.chmod(0o644)
    second = ledger.append(session(manifest, 1))

    assert first.previous_hash == "GENESIS"
    assert second.previous_hash == first.entry_hash
    assert len(ledger.load()) == 2
    assert path.stat().st_mode & 0o777 == 0o600

    content = path.read_text().replace(
        '"code_version":"commit:abc123"',
        '"code_version":"commit:def456"',
        1,
    )
    path.write_text(content)
    with pytest.raises(ReadinessEvidenceError, match="hash verification"):
        ledger.load()


def test_complete_external_and_multi_session_evidence_only_admits_read_only_shadow(
    tmp_path: Path,
) -> None:
    manifest = verified_manifest()
    ledger = ShadowLedger(tmp_path / "shadow.jsonl")
    ledger.append(session(manifest, 0, degradation=True))
    ledger.append(session(manifest, 1, recovery=True))
    ledger.append(session(manifest, 2))

    report = evaluate_readiness(
        manifest,
        ledger.load(),
        evaluated_at=NOW,
        minimum_sessions=3,
        minimum_trading_dates=3,
    )

    assert report.evidence_complete is True
    assert report.shadow_admission_ready is True
    assert report.production_ready is False
    assert report.execution_enabled is False
    assert report.action == "NO_TRADE"
    assert report.manual_webull_execution_only is True


def test_shadow_sessions_must_match_the_evaluated_runtime_artifact(tmp_path: Path) -> None:
    manifest = verified_manifest()
    ledger = ShadowLedger(tmp_path / "version-mismatch-shadow.jsonl")
    ledger.append(session(manifest, 0, degradation=True, recovery=True))

    report = evaluate_readiness(
        manifest,
        ledger.load(),
        evaluated_at=NOW,
        expected_code_version="commit:different-deployment",
        minimum_sessions=1,
        minimum_trading_dates=1,
    )

    assert report.shadow_admission_ready is False
    assert report.shadow_evidence.qualifying_sessions == 0
    assert "SHADOW:CODE_VERSION_MISMATCH" in report.blockers


def test_expired_evidence_and_manifest_mismatch_do_not_qualify(tmp_path: Path) -> None:
    manifest = verified_manifest()
    ledger = ShadowLedger(tmp_path / "shadow.jsonl")
    ledger.append(session(manifest, 0, degradation=True, recovery=True))
    report = evaluate_readiness(
        manifest.model_copy(
            update={
                "evidence": tuple(
                    item.model_copy(update={"expires_at": NOW}) for item in manifest.evidence
                )
            }
        ),
        ledger.load(),
        evaluated_at=NOW,
        minimum_sessions=1,
        minimum_trading_dates=1,
    )

    assert report.shadow_admission_ready is False
    assert report.shadow_evidence.qualifying_sessions == 0
    assert any(item.startswith("EVIDENCE_EXPIRED:") for item in report.blockers)


def test_future_shadow_session_never_satisfies_admission(tmp_path: Path) -> None:
    manifest = verified_manifest()
    payload = session(manifest, 0, degradation=True, recovery=True).model_dump()
    payload.update(
        {
            "started_at": NOW + timedelta(hours=1),
            "ended_at": NOW + timedelta(hours=2),
            "trading_date": (NOW + timedelta(hours=1))
            .astimezone(ZoneInfo("America/New_York"))
            .date(),
        }
    )
    future_session = ShadowSession.model_validate(payload)
    ledger = ShadowLedger(tmp_path / "future-shadow.jsonl")
    ledger.append(future_session)

    report = evaluate_readiness(
        manifest,
        ledger.load(),
        evaluated_at=NOW,
        minimum_sessions=1,
        minimum_trading_dates=1,
    )

    assert report.shadow_admission_ready is False
    assert report.shadow_evidence.qualifying_sessions == 0
    assert report.shadow_evidence.rejected_time_window_sessions == 1
    assert "SHADOW:SESSION_TIME_WINDOW" in report.blockers


def test_session_before_current_manifest_never_satisfies_admission(tmp_path: Path) -> None:
    manifest = verified_manifest()
    payload = session(manifest, 0, degradation=True, recovery=True).model_dump()
    started_at = manifest.generated_at - timedelta(hours=2)
    payload.update(
        {
            "started_at": started_at,
            "ended_at": started_at + timedelta(hours=1),
            "trading_date": started_at.astimezone(ZoneInfo("America/New_York")).date(),
        }
    )
    ledger = ShadowLedger(tmp_path / "pre-manifest-shadow.jsonl")
    ledger.append(ShadowSession.model_validate(payload))

    report = evaluate_readiness(
        manifest,
        ledger.load(),
        evaluated_at=NOW,
        minimum_sessions=1,
        minimum_trading_dates=1,
    )

    assert report.shadow_admission_ready is False
    assert report.shadow_evidence.qualifying_sessions == 0
    assert report.shadow_evidence.rejected_time_window_sessions == 1
    assert "SHADOW:SESSION_TIME_WINDOW" in report.blockers


def test_shadow_session_rejects_continuous_contract_and_non_safe_count() -> None:
    payload = session(verified_manifest(), 0).model_dump()
    payload["exact_es_contract"] = "ESmain"
    with pytest.raises(ValidationError, match="continuous/main ES"):
        ShadowSession.model_validate(payload)

    payload = session(verified_manifest(), 0).model_dump()
    payload["decision_count"] = 4
    with pytest.raises(ValidationError, match="NO_TRADE or WAIT"):
        ShadowSession.model_validate(payload)


def test_shadow_session_binds_trading_date_and_bounds_evidence_ids() -> None:
    payload = session(verified_manifest(), 0).model_dump()
    payload["trading_date"] = date(2026, 8, 17)
    with pytest.raises(ValidationError, match="America/New_York"):
        ShadowSession.model_validate(payload)

    payload = session(verified_manifest(), 0).model_dump()
    payload["capability_report_ids"] = ("x" * 121,)
    with pytest.raises(ValidationError, match="at most 120"):
        ShadowSession.model_validate(payload)

    payload = session(verified_manifest(), 0).model_dump()
    payload["incident_ids"] = ("invalid id with spaces",)
    with pytest.raises(ValidationError, match="pattern"):
        ShadowSession.model_validate(payload)


def test_cli_template_and_missing_shadow_remain_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "manifest.json"
    assert main(["readiness-template", "--output", str(manifest_path)]) == 2
    assert "status=UNVERIFIED" in capsys.readouterr().out

    exit_code = main(
        [
            "readiness-check",
            "--manifest",
            str(manifest_path),
            "--shadow-ledger",
            str(tmp_path / "missing.jsonl"),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert output["action"] == "NO_TRADE"
    assert output["execution_enabled"] is False


def test_cli_shadow_record_rejects_secret_or_extra_payload_field(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_path = tmp_path / "session.json"
    payload = session(verified_manifest(), 0).model_dump(mode="json")
    payload["webull_app_secret"] = "must-not-enter-ledger"
    session_path.write_text(json.dumps(payload))

    exit_code = main(
        [
            "shadow-record",
            "--ledger",
            str(tmp_path / "shadow.jsonl"),
            "--session-file",
            str(session_path),
        ]
    )

    assert exit_code == 2
    assert "INVALID_SHADOW_EVIDENCE" in capsys.readouterr().out
    assert not (tmp_path / "shadow.jsonl").exists()


def test_manifest_file_is_owner_only(tmp_path: Path) -> None:
    destination = tmp_path / "manifest.json"
    save_readiness_manifest(destination, verified_manifest())
    assert destination.stat().st_mode & 0o777 == 0o600


def test_readers_fail_closed_on_group_readable_evidence(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    save_readiness_manifest(manifest_path, verified_manifest())
    manifest_path.chmod(0o640)
    with pytest.raises(ReadinessEvidenceError, match="permissions"):
        load_readiness_manifest(manifest_path)

    ledger_path = tmp_path / "shadow.jsonl"
    ledger = ShadowLedger(ledger_path)
    ledger.append(session(verified_manifest(), 0))
    ledger_path.chmod(0o644)
    with pytest.raises(ReadinessEvidenceError, match="permissions"):
        ledger.load()


def test_evidence_files_reject_symbolic_links(tmp_path: Path) -> None:
    manifest_target = tmp_path / "manifest-target.json"
    save_readiness_manifest(manifest_target, verified_manifest())
    manifest_link = tmp_path / "manifest-link.json"
    manifest_link.symlink_to(manifest_target)
    with pytest.raises(ReadinessEvidenceError, match="regular file"):
        load_readiness_manifest(manifest_link)

    ledger_target = tmp_path / "shadow-target.jsonl"
    ledger = ShadowLedger(ledger_target)
    ledger.append(session(verified_manifest(), 0))
    ledger_link = tmp_path / "shadow-link.jsonl"
    ledger_link.symlink_to(ledger_target)
    with pytest.raises(ReadinessEvidenceError, match="regular file"):
        ShadowLedger(ledger_link).append(session(verified_manifest(), 1))
