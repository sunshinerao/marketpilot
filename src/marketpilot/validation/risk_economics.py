from __future__ import annotations

import math
from dataclasses import dataclass

from marketpilot.domain.decision import DecisionAction, NoTradeReason
from marketpilot.validation.execution_economics import ExecutableValue


def _finite(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class PnlScenario:
    name: str
    probability: float
    conservative_pnl: float

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("scenario name must not be blank")
        probability = _finite(self.probability, "probability")
        pnl = _finite(self.conservative_pnl, "conservative_pnl")
        if probability <= 0 or probability > 1:
            raise ValueError("scenario probability must be in (0, 1]")
        object.__setattr__(self, "probability", probability)
        object.__setattr__(self, "conservative_pnl", pnl)


@dataclass(frozen=True, slots=True)
class ConservativeRiskContract:
    """Risk inputs that cannot be safely inferred from a finite scenario sample.

    ``max_loss`` is a verified all-path, fee-inclusive loss bound for the proposed
    structure. ``None`` means unbounded or not proven and therefore cannot pass an
    entry risk gate.
    """

    max_loss: float | None
    risk_budget: float
    cvar_budget: float
    cvar_confidence: float = 0.95

    def __post_init__(self) -> None:
        risk_budget = _finite(self.risk_budget, "risk_budget")
        cvar_budget = _finite(self.cvar_budget, "cvar_budget")
        confidence = _finite(self.cvar_confidence, "cvar_confidence")
        if risk_budget < 0 or cvar_budget < 0:
            raise ValueError("risk budgets must not be negative")
        if not 0 < confidence < 1:
            raise ValueError("cvar_confidence must be in (0, 1)")
        if self.max_loss is not None:
            maximum = _finite(self.max_loss, "max_loss")
            if maximum < 0:
                raise ValueError("max_loss must not be negative")
            object.__setattr__(self, "max_loss", maximum)
        object.__setattr__(self, "risk_budget", risk_budget)
        object.__setattr__(self, "cvar_budget", cvar_budget)
        object.__setattr__(self, "cvar_confidence", confidence)


@dataclass(frozen=True, slots=True)
class RiskEligibility:
    action: DecisionAction
    entry_eligible: bool
    quote_executable: bool
    max_loss: float | None
    scenario_max_loss: float
    scenario_expected_pnl: float
    tail_loss_cvar: float
    reason: NoTradeReason | None


def _weighted_tail_loss_cvar(scenarios: tuple[PnlScenario, ...], confidence: float) -> float:
    tail_mass = 1.0 - confidence
    remaining = tail_mass
    tail_pnl = 0.0
    for scenario in sorted(scenarios, key=lambda item: item.conservative_pnl):
        consumed = min(remaining, scenario.probability)
        tail_pnl += consumed * scenario.conservative_pnl
        remaining -= consumed
        if remaining <= 1e-15:
            break
    return max(0.0, -tail_pnl / tail_mass)


def assess_entry_risk(
    execution: ExecutableValue,
    scenarios: tuple[PnlScenario, ...],
    contract: ConservativeRiskContract,
) -> RiskEligibility:
    """Fail-closed entry eligibility based on executable prices and bounded risk."""

    if not scenarios:
        raise ValueError("scenarios must not be empty")
    if len({scenario.name for scenario in scenarios}) != len(scenarios):
        raise ValueError("scenario names must be unique")
    total_probability = math.fsum(scenario.probability for scenario in scenarios)
    if not math.isclose(total_probability, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("scenario probabilities must sum to 1")
    expected = math.fsum(scenario.probability * scenario.conservative_pnl for scenario in scenarios)
    expected = _finite(expected, "scenario_expected_pnl")
    scenario_max_loss = max(0.0, -min(scenario.conservative_pnl for scenario in scenarios))
    cvar = _weighted_tail_loss_cvar(scenarios, contract.cvar_confidence)
    cvar = _finite(cvar, "tail_loss_cvar")
    if contract.max_loss is not None and scenario_max_loss > contract.max_loss + 1e-9:
        raise ValueError("scenario loss exceeds the declared max_loss bound")

    reason: NoTradeReason | None = None
    if not execution.is_executable:
        reason = NoTradeReason.OPTION_CHAIN_UNUSABLE
    elif (
        contract.max_loss is None
        or contract.max_loss > contract.risk_budget
        or cvar > contract.cvar_budget
    ):
        reason = NoTradeReason.RISK_BUDGET_EXCEEDED
    eligible = reason is None
    return RiskEligibility(
        # This is an economic sub-gate, never authority to enter. A full decision
        # runner may evaluate the remaining evidence gates after WAIT.
        action=DecisionAction.WAIT if eligible else DecisionAction.NO_TRADE,
        entry_eligible=eligible,
        quote_executable=execution.is_executable,
        max_loss=contract.max_loss,
        scenario_max_loss=scenario_max_loss,
        scenario_expected_pnl=expected,
        tail_loss_cvar=cvar,
        reason=reason,
    )
