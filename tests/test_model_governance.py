from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from marketpilot.domain.governance import (
    ApprovalAction,
    GovernanceApproval,
    GovernanceError,
    ModelVersion,
)
from marketpilot.governance.registry import FrozenChampionRegistry

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
MODEL_ID = "strikepilot_spxw_0dte_ic"


def version(name: str, *, parent: str | None = None, validated: bool = True) -> ModelVersion:
    return ModelVersion(
        model_id=MODEL_ID,
        version=name,
        artifact_hash=f"sha256:artifact-{name}",
        data_manifest_hash=f"sha256:data-{name}",
        trained_at=NOW,
        validation_report_hash=f"sha256:validation-{name}" if validated else None,
        parent_version=parent,
    )


def approval(
    action: ApprovalAction,
    target: str,
    *,
    source: str | None,
    evidence: str,
) -> GovernanceApproval:
    return GovernanceApproval.create(
        action=action,
        model_id=MODEL_ID,
        source_version=source,
        target_version=target,
        approved_by="risk-committee",
        approved_at=NOW,
        evidence_hash=evidence,
        note="explicit test approval",
    )


def test_unvalidated_or_unapproved_challenger_cannot_become_champion() -> None:
    registry = FrozenChampionRegistry()
    challenger = version("1.0.0", validated=False)
    registry.register_challenger(challenger)
    signed = approval(
        ApprovalAction.PROMOTE,
        "1.0.0",
        source=None,
        evidence="sha256:unrelated",
    )
    with pytest.raises(GovernanceError, match="no frozen validation"):
        registry.promote(MODEL_ID, "1.0.0", signed)


def test_session_champion_stays_frozen_across_later_promotion() -> None:
    registry = FrozenChampionRegistry()
    first = version("1.0.0")
    second = version("1.1.0", parent="1.0.0")
    registry.register_challenger(first)
    registry.promote(
        MODEL_ID,
        first.version,
        approval(
            ApprovalAction.PROMOTE,
            first.version,
            source=None,
            evidence=first.validation_report_hash or "",
        ),
    )
    assert registry.freeze_session(MODEL_ID, "2026-08-17").version == "1.0.0"

    registry.register_challenger(second)
    registry.promote(
        MODEL_ID,
        second.version,
        approval(
            ApprovalAction.PROMOTE,
            second.version,
            source=first.version,
            evidence=second.validation_report_hash or "",
        ),
    )
    assert registry.champion(MODEL_ID).version == "1.1.0"
    assert registry.champion(MODEL_ID, session_id="2026-08-17").version == "1.0.0"


def test_promotion_requires_matching_lineage_and_evidence() -> None:
    registry = FrozenChampionRegistry()
    first = version("1.0.0")
    registry.register_challenger(first)
    wrong_evidence = approval(
        ApprovalAction.PROMOTE,
        first.version,
        source=None,
        evidence="sha256:wrong",
    )
    with pytest.raises(GovernanceError, match="evidence"):
        registry.promote(MODEL_ID, first.version, wrong_evidence)


def test_explicit_rollback_preserves_lineage_and_audit_trail() -> None:
    registry = FrozenChampionRegistry()
    first = version("1.0.0")
    second = version("1.1.0", parent=first.version)
    registry.register_challenger(first)
    registry.promote(
        MODEL_ID,
        first.version,
        approval(
            ApprovalAction.PROMOTE,
            first.version,
            source=None,
            evidence=first.validation_report_hash or "",
        ),
    )
    registry.register_challenger(second)
    registry.promote(
        MODEL_ID,
        second.version,
        approval(
            ApprovalAction.PROMOTE,
            second.version,
            source=first.version,
            evidence=second.validation_report_hash or "",
        ),
    )
    rollback = approval(
        ApprovalAction.ROLLBACK,
        first.version,
        source=second.version,
        evidence="sha256:incident-review",
    )
    registry.rollback(MODEL_ID, first.version, rollback)

    assert registry.champion(MODEL_ID) == first
    assert [item.version for item in registry.lineage(MODEL_ID, second.version)] == [
        "1.1.0",
        "1.0.0",
    ]
    assert [event.action for event in registry.audit_events()] == [
        ApprovalAction.PROMOTE,
        ApprovalAction.PROMOTE,
        ApprovalAction.ROLLBACK,
    ]


def test_tampered_or_reused_approval_is_rejected() -> None:
    registry = FrozenChampionRegistry()
    first = version("1.0.0")
    registry.register_challenger(first)
    signed = approval(
        ApprovalAction.PROMOTE,
        first.version,
        source=None,
        evidence=first.validation_report_hash or "",
    )
    with pytest.raises(GovernanceError, match="identity mismatch"):
        registry.promote(MODEL_ID, first.version, replace(signed, approved_by="attacker"))

    registry.promote(MODEL_ID, first.version, signed)
    with pytest.raises(GovernanceError, match="already been used"):
        registry.promote(MODEL_ID, first.version, signed)
