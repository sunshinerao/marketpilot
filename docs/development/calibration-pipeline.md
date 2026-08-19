# Phase 5 — calibration pipeline design (spine v1)

Status: in delivery. Date: 2026-08-20. Data foundation: Phase 2 complete
(251/251 trading days of SPXW 0DTE minute NBBO, ES minute bars, definitions).

## Goal

Produce the first real calibration evidence for StrikePilot's tail model from
licensed point-in-time history: corridor coverage by regime, No Trade
frequency, and conservative economics — ending in a calibration report that
either promotes the baseline through the pre-registered gate or says NO.

## Architecture (the spine)

```text
landed DBN batches (encrypted, PIT-registered)
  → normalize (A): per-day typed structures (ChainDay)
  → implied SPX (B): Ŝ series from ES anchor model + prior cash closes
  → outcome labels (C): realized up/down max excursion entry→close
  → tail model v1 (integration): conditional empirical quantiles given IV regime
  → purged walk-forward (existing validation/walk_forward.py)
  → joint coverage + NO_TRADE frequency + conservative economics report
```

Shared contracts live in `features/day_structure.py`; every stage is a pure
function over those types so each workstream is independently testable.

## Workstreams

- **A — normalize**: `ingest/normalize.py`. Landed DBN → `ChainDay`
  (underlying ES minute bars + 0DTE chain minute NBBO quotes), strict schema
  validation (monotone timestamps, padded symbols, bid<=ask when both present).
- **B — implied SPX + anchors**: `features/implied_spx.py`. Ŝ_t ≈ SPX_a ×
  F_t / F_a with prior official cash closes from a free public source
  (Massive Basic I:SPX EOD preferred, Cboe CSV fallback), anchor series
  registered with provenance.
- **C — outcome labels**: `validation/realized_excursions.py`. Per day and
  candidate entry time, realized up/down max excursion from entry to the
  versioned session close, honoring the strict post-cutoff label contract.
- **Integration (orchestrator)**: tail model v1 (conditional empirical
  quantiles by IV regime — the simplest honest baseline), purged walk-forward,
  calibration report.

## Non-goals for v1

No Greeks-based features yet (they exist in the data; v1 keeps features
minimal and inspectable), no ML model, no Massive subscription requirement
(anchors come from free sources; intraday SPX stays implied), no live path
changes.

## Honesty rules

- Every label is computed only from data visible at the as-of replay instant
  (`ReplayVisibility.AVAILABLE` semantics).
- The calibration report must state its window, gaps, and anchor source; any
  approximation (implied SPX) is labelled, never presented as official SPX.
- Promotion remains blocked until the pre-registered criteria pass on the
  untouched holdout; a failed calibration is a successful, reportable outcome.
