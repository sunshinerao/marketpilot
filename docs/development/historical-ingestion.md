# Phase 2 — historical ingestion design (pending review)

Status: **design proposal, awaiting owner review before the first licensed pull.**
Date: 2026-08-18. Sources and prices: see
[data-source-evaluation.md](data-source-evaluation.md) (verified 2026-08-18).

## 1. Purpose

Build the immutable point-in-time history that StrikePilot calibration, purged
walk-forward validation, and ScoutPilot detector calibration all read from. This
phase pulls **licensed historical data** from Databento into the encrypted raw
landing boundary, normalizes it into point-in-time batch records, and emits
deterministic replay manifests. It does not touch the live Webull path, does not
stream real-time data, and does not redistribute anything.

## 2. Source-of-truth map

| Data | Source for history | Schema | Live path (unchanged) |
|---|---|---|---|
| SPXW chain minute NBBO (incl. expired) | Databento OPRA.PILLAR | `cbbo-1m` | Webull OPRA (verified) |
| SPXW contract definitions | Databento OPRA.PILLAR | `definitions` | Webull discovery |
| ES futures minutes | Databento GLBX.MDP3 | `ohlcv-1m` | Deferred (no CME) |
| SPX/VIX/VIX1D EOD | Cboe free CSVs | CSV | Massive Indices after subscription |
| SPX/VIX intraday | Massive (post-subscription, from 2023-03) | — | Massive Indices Advanced |

ES tick-level `trades` (~$0.34/day) are excluded from the initial pull; minute
bars suffice for calibration. If walk-forward later shows bar-level fills too
coarse, a targeted trades pull is a separate, costed decision.

## 3. Initial pull scope and cost ceiling

- **Window**: the most recent 12 months of trading days.
- **Symbols**: full SPXW chain per day (all expirations listed that day; 0DTE
  chains are the calibration target, but the whole chain is pulled so detectors
  can study term structure later), plus the explicit front-month ES contract
  series per day.
- **Estimated cost** (from verified unit prices): OPRA cbbo-1m ≈ $0.03/day →
  12 months ≈ **$8**; ES ohlcv-1m ≈ $0.005/day → ≈ **$1**. Fits the Databento
  $125 new-user credit with an order of magnitude of headroom.
- **Hard ceiling**: every pull must call `metadata.get_cost` first, record the
  estimate in the pull manifest, and abort when the estimate exceeds the
  configured ceiling (`MARKETPILOT_PULL_MAX_COST_USD`, default `25`). The cost
  ledger is append-only and every pull references it.

## 4. Storage layout

```text
data/raw/                              # gitignored; encrypted landing boundary
  databento/OPRA.PILLAR/cbbo-1m/YYYY-MM-DD.dbn.enc
  databento/OPRA.PILLAR/definitions/YYYY-MM-DD.dbn.enc
  databento/GLBX.MDP3/ohlcv-1m/YYYY-MM-DD.dbn.enc
data/derived/pit/                      # gitignored; normalized batch records
```

- Raw day files are written through `services/raw_landing.py` (local cipher for
  now; KMS/HSM remains a production gate listed in the safety MVP acceptance).
- **PIT granularity is one record per dataset/schema/symbol/day**, not per tick.
  Each record carries the batch content hash, row count, and min/max event
  timestamps. Tick-level point-in-time correctness inside a batch comes from
  Databento's event timestamps; cross-day correctness comes from the replay
  clock over batch records. This keeps the ledger at ~10⁵ records/year instead
  of ~10⁹.
- Replay manifests reference batch records; a backtest as-of date resolves to
  exactly the set of day batches visible at that instant.

## 5. Point-in-time semantics

- `published_at` = end of the data day (23:59:59 ET), a conservative proxy for
  when the batch could have been available; `first_seen_at` = actual pull time.
  The record invariant (`published_at <= first_seen_at`) therefore holds for
  any pull after the day closes; a same-day pull attempt fails validation.
- Instrument definitions are their own record family, versioned per day, so an
  as-of replay sees the contracts that existed that day — including contracts
  that have since expired.
- Databento days flagged as degraded are recorded as `AMBER` data-quality
  facts; they are never silently treated as complete.
- Features and outcome labels may only read through the virtual replay clock.
  Direct raw-file reads outside the replay path are a contract violation.

## 6. Integrity and idempotency

- Re-pulling the same window must produce identical content hashes; a
  mismatch fails the audit instead of overwriting.
- Every batch records row count and min/max event timestamps; the trading
  calendar (`domain/trading_calendar.py`) supplies expected trading days, and
  missing days are listed explicitly in the pull manifest (holiday vs gap).
- All receipts land in the append-only audit store; the encrypted payload never
  appears in receipts, logs, or the repository.

## 7. Failure handling

- Day-file atomicity: a day batch is either fully landed and recorded or
  absent; partial files are quarantined and reported, never partially visible.
- Rate limits and transient errors retry with backoff inside one pull run; a
  permanently failing day is recorded as a gap with its error class.
- A pull can be re-run for exactly the missing days (gap-fill mode); it never
  rewrites a landed day.

## 8. Work packages

- [ ] WP1 — Databento adapter contract: historical client wrapper (cost
  estimation, batch download, dataset/schema registry), fake-client tests,
  no live calls in unit tests.
- [ ] WP2 — Pull orchestrator: trading-calendar day expansion, cost gate,
  gap-fill mode, append-only cost ledger.
- [ ] WP3 — Landing writer: encrypted day files, receipts, quarantine path.
- [ ] WP4 — PIT batch records + replay manifest emission over pulled windows.
- [ ] WP5 — Integrity audit: hash-stability re-pull check, gap report,
  calendar reconciliation.
- [ ] WP6 — First calibration pull (12-month window). **Requires owner
  approval: spends part of the Databento free credit.**

## 9. Review questions for the owner

1. Initial window: 12 months acceptable, or go straight to 3 years (≈ $25)?
2. Cost ceiling: is $25 per pull the right default?
3. Whole-chain pull (recommended, enables vol-squeeze/gamma research) vs
   0DTE-only chains (cheaper, narrower)?
4. Confirm the PIT granularity decision (batch-per-day records, tick PIT via
   Databento event timestamps inside the batch).
