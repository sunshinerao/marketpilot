from __future__ import annotations

from marketpilot.domain.governance import (
    ApprovalAction,
    GovernanceApproval,
    GovernanceError,
    GovernanceEvent,
    ModelVersion,
)


class FrozenChampionRegistry:
    """Keeps production champions frozen while challengers remain offline."""

    def __init__(self) -> None:
        self._versions: dict[str, dict[str, ModelVersion]] = {}
        self._champions: dict[str, str] = {}
        self._session_champions: dict[tuple[str, str], str] = {}
        self._events: list[GovernanceEvent] = []
        self._used_approvals: set[str] = set()

    def register_challenger(self, model: ModelVersion) -> None:
        versions = self._versions.setdefault(model.model_id, {})
        if model.version in versions:
            raise GovernanceError(f"model version already registered: {model.version}")
        if model.parent_version is not None and model.parent_version not in versions:
            raise GovernanceError(f"unknown parent version: {model.parent_version}")
        versions[model.version] = model

    def promote(self, model_id: str, version: str, approval: GovernanceApproval) -> ModelVersion:
        candidate = self._get(model_id, version)
        current = self._champions.get(model_id)
        self._validate_approval(
            approval,
            action=ApprovalAction.PROMOTE,
            model_id=model_id,
            source_version=current,
            target_version=version,
        )
        if candidate.validation_report_hash is None:
            raise GovernanceError("challenger has no frozen validation report")
        if approval.evidence_hash != candidate.validation_report_hash:
            raise GovernanceError("promotion evidence does not match validation report")
        if approval.approved_at < candidate.trained_at:
            raise GovernanceError("promotion approval predates the challenger")
        if current is not None and candidate.parent_version != current:
            raise GovernanceError("challenger lineage must descend from the current champion")

        self._champions[model_id] = version
        self._record(approval)
        return candidate

    def freeze_session(self, model_id: str, session_id: str) -> ModelVersion:
        if not session_id.strip():
            raise GovernanceError("session_id must not be blank")
        current = self._champions.get(model_id)
        if current is None:
            raise GovernanceError(f"model has no champion: {model_id}")
        frozen_version = self._session_champions.setdefault((model_id, session_id), current)
        return self._get(model_id, frozen_version)

    def champion(self, model_id: str, *, session_id: str | None = None) -> ModelVersion:
        if session_id is not None:
            frozen = self._session_champions.get((model_id, session_id))
            if frozen is not None:
                return self._get(model_id, frozen)
        version = self._champions.get(model_id)
        if version is None:
            raise GovernanceError(f"model has no champion: {model_id}")
        return self._get(model_id, version)

    def rollback(
        self,
        model_id: str,
        target_version: str,
        approval: GovernanceApproval,
    ) -> ModelVersion:
        target = self._get(model_id, target_version)
        current = self._champions.get(model_id)
        if current is None:
            raise GovernanceError(f"model has no champion: {model_id}")
        if target_version == current:
            raise GovernanceError("rollback target is already the champion")
        self._validate_approval(
            approval,
            action=ApprovalAction.ROLLBACK,
            model_id=model_id,
            source_version=current,
            target_version=target_version,
        )
        self._champions[model_id] = target_version
        self._record(approval)
        return target

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
        return tuple(self._events)

    def versions(self, model_id: str) -> tuple[ModelVersion, ...]:
        versions = self._versions.get(model_id, {})
        return tuple(sorted(versions.values(), key=lambda item: (item.trained_at, item.version)))

    def _get(self, model_id: str, version: str) -> ModelVersion:
        try:
            return self._versions[model_id][version]
        except KeyError as exc:
            raise GovernanceError(f"unknown model version: {model_id}@{version}") from exc

    def _validate_approval(
        self,
        approval: GovernanceApproval,
        *,
        action: ApprovalAction,
        model_id: str,
        source_version: str | None,
        target_version: str,
    ) -> None:
        approval.verify()
        if approval.approval_id in self._used_approvals:
            raise GovernanceError("governance approval has already been used")
        expected = (action, model_id, source_version, target_version)
        actual = (
            approval.action,
            approval.model_id,
            approval.source_version,
            approval.target_version,
        )
        if actual != expected:
            raise GovernanceError("approval does not match the requested governance action")

    def _record(self, approval: GovernanceApproval) -> None:
        self._used_approvals.add(approval.approval_id)
        self._events.append(
            GovernanceEvent(
                action=approval.action,
                model_id=approval.model_id,
                source_version=approval.source_version,
                target_version=approval.target_version,
                approval_id=approval.approval_id,
                occurred_at=approval.approved_at,
            )
        )
