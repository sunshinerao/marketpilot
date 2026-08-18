# MarketPilot delivery roadmap

This roadmap converts the design document and product decisions into independently
verifiable work packages. A later phase may be scaffolded early, but it cannot be
promoted while an earlier safety gate remains open.

## Phase 1 — provider capability evidence

- [x] Official Webull SDK adapter with lazy credential loading.
- [x] Explicit-contract guard; no `ESmain` substitution.
- [x] Redacted probe report, versioned local persistence, CLI, and read-only API.
- [x] Account market-data subscription probe plus SDK-surface coverage findings;
  SPX/VIX index feeds recorded as not offered by Webull OpenAPI.
- [x] Unit, contract, secret-leakage, installation, CLI, and API smoke tests.
- [x] Provider-neutral external-evidence manifest and fail-closed shadow-admission gate.
- [x] Data-source evaluation for ES/SPX/VIX/expired SPXW/short-interest coverage:
  [data-source-evaluation.md](data-source-evaluation.md).
- [ ] Run against the authorized Webull OpenAPI account and record entitlement evidence.
  Progress 2026-08-18: production probe run; credentials, account subscriptions, and
  all three SPXW/OPRA legs PASS at schema level (report v2, redacted). Remaining:
  entitlement-scope mapping into the readiness manifest, multi-sample latency, and
  timestamp-semantics review.
- [ ] Confirm independent/licensed coverage for SPX, VIX/VIX1D, expired SPXW NBBO, and
  event/news/social sources where Webull does not provide point-in-time coverage.

Exit gate: all required instruments, timestamps, delayed flags, historical depth, and
license scopes are evidenced. A Green provider report still does not enable execution.

## Phase 2 — immutable point-in-time data and replay

- [x] Historical ingestion design:
  [historical-ingestion.md](historical-ingestion.md) (awaiting owner review before
  the first licensed pull).
- [x] Immutable point-in-time contract with `published_at`, `first_seen_at`, provider version, and
  content hash.
- [x] Virtual replay clock enforcing strict `as_of` reads and revision visibility.
- [x] Outcome-label contract rejects pre-cutoff data and requires an exact expiry window.
- [ ] Run outcome-label generation on authorized licensed history.
- [x] Leakage tests plus deterministic replay manifests.
- [x] Authorized-first encrypted raw-payload boundary and PostgreSQL safe-receipt adapter.
- [ ] Production KMS/HSM, immutable object-store IAM/retention, and backup/restore drill.

Exit gate: a replay can be reproduced from a manifest and fails when future or revised
data crosses the cutoff.

## Phase 3 — event intelligence and Risk Lock

- [x] Event/Risk Lock domain and deterministic fixtures for scheduled, social, geopolitical, disaster,
  and unscheduled market shocks.
- [x] Event windows including pre-event, second/minute/hour post-event, relevant session
  close, and next cash-open checkpoints; missing session boundaries remain explicit.
- [x] Evidence corroboration, cross-asset confirmation, stability/hysteresis, and explicit
  `NO_TRADE` gates. Headlines never directly create a directional order.
- [x] Version-bounded US equity/Globex calendar, DST, holiday/half-day, explicit ES
  contract, and dual-source quote-quality contracts.
- [ ] Authorized live event/news/social collectors and production calendar orchestration.

Exit gate: event replays demonstrate timestamp correctness, deduplication, and safe
behavior under contradictory or late evidence.

## Phase 4 — bidirectional alerts and reverse attribution

- [x] Local resumable SSE stream with `Last-Event-ID`, heartbeat, append-only delivery
  attempts, and disconnect/failure replay tests.
- [ ] Opt-in mobile, email, and webhook delivery channels.
- [x] No-network WEBHOOK/EMAIL/MOBILE outbox harness with signatures, retry/backoff,
  dead-letter, acknowledgment, escalation, and SSRF-safe opaque destinations.
- [x] Local deduplication, cooldown, hysteresis, acknowledgment, escalation, and audit contracts.
- [x] User feedback captured as append-only local evidence and exposed in the workbench.
- [x] Scenario-local major-event/abnormal-move reverse attribution, review workflow, and
  counterfactual replay link.
- [ ] Wire authorized live triggers to automatic attribution task creation.

Exit gate: reconnect, duplicate delivery, acknowledgment, and escalation paths pass
failure-injection tests.

## Phase 5 — strategy validation and controlled improvement

- [x] Purged walk-forward and event/regime-stratified evaluation contracts.
- [x] Conservative NBBO/size/spread/slippage/fee, executable PnL, max-loss, EV, CVaR,
  and risk-budget contracts with fail-closed API.
- [ ] Produce calibrated reports on licensed point-in-time data.
- [x] Decision-path frozen intraday champion with server-owned LIVE sessions, isolated
  explicit SCENARIO sessions, and exact loaded version plus declared-hash matching.
- [x] Explicit local promotion approval, model/rules/data versioning, and rollback.
- [x] Versioned local pre-registration criteria and tamper-evident holdout gate contract.
- [ ] Pre-registered production thresholds and untouched holdout approval evidence.

Exit gate: a challenger is promoted only after predefined risk and performance criteria
pass on untouched data; the running model never rewrites itself intraday.

## Phase 6 — shadow operation

- [ ] Live read-only decisions and manual execution through Webull.
- [x] Local decision, replay, event, alert, feedback, governance, stream, and attribution
  audit contracts.
- [x] PostgreSQL runtime parity and local real-database migration/trigger/restart exercise.
- [x] Read-only shadow runbook, source-degradation drill, audit integrity check, and
  rollback procedure.
- [x] Hash-chained, append-only shadow-session ledger and multi-session evidence report.
- [ ] Complete an authorized multi-session shadow observation and recovery drill.

Automated order submission is outside the current authorization boundary.

## Phase 7 — opportunity discovery (ScoutPilot scanner family)

Per ADR 0003, detectors emit evidence-bounded candidates, never decisions. Ordered
by data availability; a detector starts only after its calibration data exists.

- [x] ScoutPilot plugin contract: versioned detector interface, candidate schema
  (target, direction, evidence, confidence, invalidation, next checkpoint), and
  universe declaration (`domain/scout.py`; inferred kinds must declare an
  estimate method).
- [ ] Daily point-in-time collection into the immutable store, starting before any
  detector: SPXW chain snapshots via the verified Webull OPRA legs; VIX complex
  once an index source is licensed (Phase 1).
- [ ] IV-crush risk detector: event calendar (earnings, FOMC, CPI) × IV percentile ×
  historical post-event realized moves; warns on long-premium exposure into events.
- [ ] Volatility-squeeze detector: IV rank/percentile compression, realized-vol
  compression, and term-structure state on the SPX complex.
- [ ] Gamma-squeeze detector: chain OI/Greeks aggregation and estimated dealer gamma
  exposure with gamma-flip level, labelled as model-based inference (method and
  input manifest travel with every candidate).
- [ ] Short-squeeze detector: short interest, borrow-rate proxy, and float rotation;
  gated on external short-interest/borrow data licensing (see the data-source
  evaluation).
- [ ] Per-detector calibration on point-in-time history with pre-registered
  promotion criteria; uncalibrated detectors emit nothing.

Exit gate: a promoted detector emits candidates carrying evidence, invalidation
conditions, and a rerun checkpoint; candidates flow through the alert pipeline; no
candidate relaxes a decision gate, and `execution_enabled` remains `false`.

## Phase 8+ — later model family

BasisPilot, FuturesPilot, EventPilot, and VolPilot decision models remain planned
plugins (design document v1.1 §1.1). Their scheduling follows the Phase 1 data
decisions and the Phase 6 shadow outcome.
