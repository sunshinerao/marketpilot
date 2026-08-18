# ADR 0003: Opportunity discovery emits candidates, never decisions

- Status: Accepted
- Date: 2026-08-18

## Context

MarketPilot gains an opportunity-discovery capability: detectors for gamma
squeezes, short squeezes, volatility squeezes, and IV-crush risk. Their outputs
are probabilistic and partly model-inferred — dealer positioning, for example,
is not observable and must be estimated from open interest and flow. Treating
such output as a decision would bypass the hard gates that define the platform
(ADR 0002) and would present estimates as facts.

## Decision

Detectors form a scanner family (`ScoutPilot`) behind one versioned plugin
contract. A detector run emits **candidate** objects only: target, direction,
evidence list, confidence, invalidation conditions, and a next-checkpoint time.
Candidates enter the existing alert pipeline (deduplication, hysteresis,
acknowledgment, escalation) and may inform a human reviewer or feed a decision
model's input gates, but they never become a decision brief by themselves and
never relax or skip a gate.

Inferred quantities (estimated dealer gamma exposure, squeeze probabilities)
are labelled as estimates and carry their method version and input data
manifest. Every detector is calibrated on point-in-time history and promoted
through the same pre-registered criteria, explicit approval, and rollback
machinery as any decision model. Data collection precedes detector claims: a
detector may not emit live candidates before its calibration dataset exists.

## Consequences

- No new execution path is introduced; the safety boundary is unchanged.
- Alert, attribution, governance, and promotion-gate machinery is reused
  instead of duplicated.
- A noisy or uncalibrated detector fails closed: no calibration, no candidates.
- Adding a detector is adding a plugin, not changing the platform contract.
