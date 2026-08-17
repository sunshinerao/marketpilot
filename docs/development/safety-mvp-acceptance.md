# Local safety MVP acceptance

## Delivered boundary

This work package delivers a visible, locally usable decision-support system. It does
not claim production data verification, calibrated strategy performance, or permission
to automate Webull execution.

Delivered capabilities:

- Fail-closed `LIVE` decisions whose timestamp, XNYS/New York session identity, gates,
  and values are derived from server-owned state; caller-supplied LIVE timestamps or
  session identifiers are rejected.
- Explicit, non-executable `SCENARIO` runs with required isolated scenario session IDs
  for controlled demonstrations.
- Immutable point-in-time records, revision visibility, virtual replay clock, and
  tamper-evident deterministic manifests.
- Event timelines with first-seen semantics, evidence corroboration, cross-asset
  confirmation, stability windows, checkpoints, and Risk Lock.
- Bidirectional tail-risk alert contracts with hysteresis, deduplication, cooldown,
  acknowledgment, dismissal, feedback, and escalation.
- Resumable local SSE alert projection with heartbeat, `Last-Event-ID`, append-only
  delivery-attempt evidence, disconnect replay, and injected-write-failure coverage.
- Scenario-local reverse attribution for major events and abnormal moves, with
  point-in-time cause evidence, cross-asset coherence, human review, reusable samples,
  and counterfactual replay links.
- Version-bounded New York equity/Globex calendars, DST and half-day behavior, explicit
  dated ES contracts, and dual-source timestamp/entitlement/quote-quality evaluation.
- Strict post-cutoff outcome labels plus conservative NBBO, size, spread, slippage,
  fees, executable PnL, max-loss, EV, CVaR, and risk-budget contracts.
- Frozen champion/offline challenger governance with explicit promotion and rollback,
  visible local challenger registration, session champion freezing, lineage, durable
  PostgreSQL audit events, decision-path session freezing, and a fail-closed exact
  version plus declared-artifact-hash consistency check.
- Purged walk-forward splitting and event/regime/`NO_TRADE` effect metric contracts.
- Responsive operator workbench with Live fail-closed, CPI Risk Lock, and event-cleared
  review-only scenarios, replay manifests, Risk Lock evidence, and persistent local
  alert feedback. There is no order button or order-submission API.
- Append-only SQLite audit persistence for decisions, alerts/feedback, point-in-time
  metadata, and replay manifests; snapshot/model/rules/code provenance travels with
  every decision. A read-only integrity command verifies schema and append-only
  triggers without creating a missing database.
- PostgreSQL runtime selection, a 16-table migration (15 audited tables plus one schema
  marker) with 30 append-only denial triggers, parity adapters for
  core audit/SSE/attribution/governance, and an encrypted licensed-payload landing
  boundary that stores only safe receipt metadata in PostgreSQL.
- Provider-neutral collector orchestration and a no-network alert-delivery outbox
  harness with deterministic failure injection.

## Local acceptance

Run the complete quality gate:

```bash
make check
```

Run the system:

```bash
make dev-api
make dev-web
```

In the workbench, verify:

1. Live / unverified returns `NO_TRADE` with server-derived blocking reasons.
2. CPI Risk Lock returns `NO_TRADE` with event/tail reasons and no strikes.
3. Event-cleared map returns `WAIT`, four review-only strikes, snapshot/model/rules
   provenance, and still shows `DEMO / UNVERIFIED` plus `MANUAL ONLY`.
4. Stop the API and rerun a scenario; the interface stays `NO_TRADE` and reports the
   connection failure.
5. Acknowledge or dismiss the demo alert, restart the API, and verify that the local
   audit status and feedback history remain available.
6. Confirm the assurance cards show a half-day actual-close anchor, a frozen dual-source
   conflict, and conservative local economics that remain `WAIT / UNVERIFIED`.
7. Verify a missing SQLite append-only trigger makes `audit-check` fail without
   recreating it, and a PostgreSQL UPDATE is rejected by the database trigger.
8. Register/promote a local challenger and confirm decisions become `NO_TRADE` with
   `MODEL_VERSION_NOT_LOADED` until that exact governed artifact is loaded or rolled back.
9. Reuse one scenario session across a later promotion and confirm it retains its frozen
   champion; a new scenario session sees the promoted identity and fails closed when that
   exact version/hash is not loaded. Confirm LIVE rejects a supplied `as_of` or session.

Verify the derived audit store before and after the restart:

```bash
marketpilot audit-check --database data/audit/marketpilot.sqlite3
```

## Gates that code cannot self-certify

The following remain required before authorized shadow operation:

- Run the Webull capability probe against the intended account and exact dated
  contracts; verify entitlement, exchange timestamps, delayed flags, NBBO fields,
  sizes, history depth, reconnect behavior, and rate limits.
- License authoritative point-in-time coverage for SPX, VIX/VIX1D, expired SPXW NBBO,
  scheduled events, news, and social sources where Webull is incomplete.
- Provision production PostgreSQL with TLS and separate migration/runtime roles; connect
  a real KMS/HSM cipher and immutable object store, then complete backup/restore,
  corruption, IAM, and retention drills. The included local PostgreSQL and raw-landing
  contracts are not infrastructure certification.
- Run shared/production mode behind a TLS identity-aware gateway, inject role-scoped
  credentials from a secret manager, and evidence rate limiting, CSRF protection,
  credential rotation/revocation, and redacted access-log retention. The in-process
  read-only/operator/reviewer boundary is implemented but does not certify that external
  ingress.
- Pin `MARKETPILOT_CODE_VERSION` to the reviewed build and bind promotion evidence to a
  signed build manifest. The current model `artifact_hash` is a deterministic declared
  executable-contract identity for governance compatibility, not binary/source-byte
  attestation by itself.
- Train and validate a challenger on immutable licensed history using predefined,
  untouched test thresholds; explicitly approve promotion. No performance claim exists
  in this repository today.
- Complete a read-only shadow period and operational degradation/rollback exercises.

Track these external gates with the owner-only readiness manifest and hash-chained
shadow ledger described in
[`../operations/readiness-evidence.md`](../operations/readiness-evidence.md). The
machine gate validates evidence structure, expiry, authority, linkage, session counts,
and drills; it cannot manufacture or self-certify the underlying account, license,
infrastructure, holdout, or observation evidence.

Until those gates pass, `execution_enabled` remains `false`, the production champion is
not calibrated, and all real execution remains a separate manual action in Webull.
