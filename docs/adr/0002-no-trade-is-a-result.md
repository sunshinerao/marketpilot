# ADR 0002: No Trade is a first-class decision result

- Status: Accepted
- Date: 2026-08-15

## Decision

`ENTER`, `WAIT`, and `NO_TRADE` are normal decision actions. Failed quality, event,
contract, option-chain, edge, and risk-budget gates return stable reason codes rather
than exceptions or fabricated values.

The result records snapshot ID, model version, rules version, data-as-of timestamp, and
code commit when available. Missing executable quotes may still produce an Amber risk
corridor, but never exact credit or executable legs.

