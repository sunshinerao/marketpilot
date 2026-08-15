# ADR 0001: Public platform and model-plugin boundary

- Status: Accepted
- Date: 2026-08-15

## Context

MarketPilot must support multiple asset classes and decision models. StrikePilot is the
first plugin, not the platform itself. Provider fields and SPXW-specific concepts must
not leak into shared domain objects.

## Decision

Shared modules own instruments, sessions, timestamps, data quality, events, snapshots,
model registration, decision runs, common risk gates, and audit metadata. Each model
plugin declares its input/output contract, asset scope, labels, calibration method, and
invalidation rules.

All provider integrations implement adapter protocols. Provider payloads are normalized
before entering the domain. A model is selected only by `model_id`; the common decision
runner does not branch on provider names or SPXW fields.

## Consequences

- Adding a provider does not change model logic.
- Adding a model does not change the public market-data contract.
- Model-specific inputs may evolve independently behind a versioned contract.
- Capability probing is required before production adapters interpret provider fields.

