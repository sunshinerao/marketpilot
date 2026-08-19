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
- **Symbols**: SPXW chains plus the front-month ES continuous series (`ES.v.0`).
- **Verified prices** (live `get_cost`, 2026-08-18): whole-parent SPXW.OPT cbbo-1m
  = **$1.07/day** (≈ $267 for 12 months — the parent selection includes every
  listed expiration); single-contract cbbo-1m ≈ $0.00003/day; per-day chain
  enumeration via the `definition` schema ≈ $0.034/day; ES continuous ohlcv-1m
  ≈ $0.005/day.
- **Strategy**: the owner-approved 0DTE enumeration strategy is implemented and
  is the CLI default (`--strategy 0dte`): each day's chain is enumerated from
  the `definition` schema (live-verified `expiration` column; a 680-contract
  0DTE chain was enumerated for 2026-08-14) and only those contracts are pulled
  as `raw_symbol` batches. Whole-parent remains available via
  `--strategy whole-chain`. Note that `ingest-plan` in 0dte mode downloads the
  per-day definition CSVs (~$0.034/day) to enumerate; they land as auditable
  PIT batches, so planning is not free but is accounted and idempotent.
- **Hard ceiling**: every pull must call `metadata.get_cost` first, record the
  estimate in the hash-chained cost ledger (`APPROVED`/`BLOCKED`), and abort when
  the plan total exceeds the configured ceiling (`DATABENTO_MAX_COST_USD`,
  default `25`, overridable per run with `--max-cost`).

## 4. Storage layout

```text
data/raw/                              # gitignored; encrypted landing boundary
  licensed/databento/<dataset>/<content-addressed object>.json
  _keys/local-aesgcm-v1.key            # local dev cipher key (KMS/HSM in production)
  _meta/receipts.jsonl                 # safe landing receipts (no payload material)
  _meta/cost-ledger.jsonl              # hash-chained cost-gate decisions
data/derived/pit/batch-records.jsonl   # gitignored; normalized PIT batch records
```

- Raw day files are written through `services/raw_landing.py` with the local
  AES-256-GCM cipher (`ingest/local_landing.py`); object keys are
  content-addressed by landing identity, so a duplicate landing is idempotent by
  construction.
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
- **Replay visibility is explicit**: `ReplayVisibility.OBSERVED` (first_seen_at)
  replays what this system actually knew — right for live-collected data;
  `ReplayVisibility.AVAILABLE` (published_at) replays what could have been
  known — required for backtests over backfilled history, where the bulk pull
  time would otherwise hide everything before the pull. Backtest manifests
  default to `AVAILABLE`.

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

- [x] WP1 — Databento adapter contract: historical client wrapper (cost
  estimation, batch download, dataset/schema registry), fake-client tests,
  no live calls in unit tests (`adapters/databento.py`).
- [x] WP2 — Pull orchestrator: trading-calendar day expansion, cost gate,
  idempotent skip, gap recording (`ingest/pipeline.py`).
- [x] WP3 — Landing writer: local AES-256-GCM cipher, content-addressed
  filesystem store, JSONL receipts, static authorizer (`ingest/local_landing.py`).
- [x] WP4 — PIT batch records + replay manifest emission over pulled windows
  (`ingest/pit_ledger.py`, `ReplayVisibility`).
- [x] WP5 — Integrity audit: gap report and calendar reconciliation
  (`ingest/audit.py`, `marketpilot ingest-audit`). Hash-stability re-pull
  verification rides on content-addressed landing idempotency.
- [x] WP6a — 0DTE chain enumeration strategy (default): per-day `definition`
  CSV enumeration with audit-landed definitions, POST form-body metadata calls
  (live-verified 414 fix for long symbol lists), `EMPTY_CHAIN` days cost nothing
  beyond enumeration, and already-landed days skip re-enumeration.
- [x] WP6 — First calibration pull (12-month window, `--strategy 0dte`).
  Completed 2026-08-20: **251/251 trading days landed** for `spxw-0dte` and
  `es-front-month` plus 251 definition batches (758 total with the validation
  week); `ingest-audit` PASS on both scopes with zero missing and zero corrupt
  records; ≈ **$21.51** estimated spend within the $125 free credit; real NBBO
  spot-checked with dual `ts_event`/`ts_recv` timestamps present. The phase-2
  data foundation is complete.

## 9. Review questions for the owner

1. Initial window: 12 months acceptable, or go straight to 3 years (≈ $25)?
2. Cost ceiling: is $25 per pull the right default?
3. Whole-chain pull (recommended, enables vol-squeeze/gamma research) vs
   0DTE-only chains (cheaper, narrower)?
4. Confirm the PIT granularity decision (batch-per-day records, tick PIT via
   Databento event timestamps inside the batch).
