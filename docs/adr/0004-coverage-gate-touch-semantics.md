# ADR 0004: The coverage gate uses touch semantics, not expiry-cross

- Status: Accepted
- Date: 2026-08-20

## Context

Calibration work produced two coverage lenses for the tail corridor:

- **Touch coverage**: no intraday breach of the recommended corridor from entry
  to close (excursion-based, the stricter measure).
- **Expiry-cross coverage**: settlement close stays inside the short strikes.

Real data shows they differ materially: at the 13:00 entry, touch coverage was
91.0% while expiry-cross coverage was 96.8% — 5.9% of days breached intraday
but settled back inside, profitable at expiry.

A positive-EV configuration (13:00, net +0.086 pts/day) exists that only looks
near-target under the expiry-cross lens. The promotion gate needs one
semantics, chosen by the owner.

## Decision

The promotion gate uses **touch coverage**: from entry to close, the
recommended corridor must not be breached intraday. A model is promotable only
when its out-of-sample touch coverage meets the pre-registered target
(currently 0.975) **and** its net (fee-aware) EV is positive.

Expiry-cross coverage is still computed and reported as secondary evidence —
it is informative about settlement outcomes, but it can never promote a model
by itself.

## Rationale

Execution is manual. An intraday breach creates margin pressure and forces a
hold-or-cut decision under stress, exactly the situation the platform exists
to prevent. A model that "usually comes back by close" is asking the operator
to absorb process risk the design document explicitly bounds (single-side
touch ≤ 0.10, expiry-cross ≤ 0.05). Optimizing for settlement luck instead of
process stability would betray the platform's founding principle: No Trade is
a first-class result.

## Consequences

- The 13:00 positive-EV configuration does **not** qualify for promotion until
  its touch coverage reaches target on out-of-sample data.
- Calibration reports present both lenses side by side; the gate reads only
  the touch column.
- Any future proposal to relax this decision requires a new ADR.
