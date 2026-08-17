# MarketPilot read-only shadow runbook

This runbook applies only to read-only decision support. It never authorizes automated
order creation, staging, routing, cancellation, or adjustment. Any Webull action remains
a separate manual action by the operator.

## Start-of-session gate

Before each US cash session:

1. Record the pinned deployed `code_version`, `rules_version`, exact model version plus
   declared artifact hash, and data-source capability report IDs.
2. Confirm the exact dated ES contract and the applicable SPXW expiration. Continuous
   main contracts are prohibited.
3. Verify source entitlements, delayed flags, exchange timestamps, quote freshness,
   bid/ask ordering, sizes, and the configured trading calendar.
4. Confirm scheduled events and next-event holding-window status. An unavailable event
   source keeps Risk Lock engaged.
5. Confirm the decision path has frozen the champion under the server-derived
   `LIVE:XNYS:<New-York-date>` session. Intraday model promotion is prohibited. A
   weekend/holiday label does not imply an open session; calendar/data/event gates still
   fail closed.
6. Confirm `execution_enabled=false` and perform a `NO_TRADE` fail-closed scenario.

If any item is missing or contradictory, remain `NO_TRADE` and open a source-degradation
incident.

Create and verify the machine-readable external-evidence manifest before recording a
session. The procedure and field constraints are defined in
[`readiness-evidence.md`](readiness-evidence.md). A new template is deliberately
`UNVERIFIED` and does not pass the gate:

```bash
marketpilot readiness-template --output data/readiness/readiness-manifest.json
marketpilot readiness-check \
  --manifest data/readiness/readiness-manifest.json \
  --shadow-ledger data/readiness/shadow-sessions.jsonl
```

## Decision review

For every brief, verify:

- `run_mode`, verification state, action, and all blocking reasons;
- exact source and receipt timestamps plus governance session, model version/artifact,
  snapshot/rules/code provenance;
- event state, corroboration, cross-asset reaction, stability window, and next rerun;
- quote age, NBBO size, spread, fees, slippage, maximum loss, and invalidation rules;
- that no order was created by MarketPilot.

`WAIT` means review-only. It is not permission to trade. `NO_TRADE` is a valid result and
must never be manually overwritten by deleting evidence or changing timestamps.

At session close, write a redacted summary and append it with `marketpilot
shadow-record`. Do not edit ledger rows: the sequence and hash chain make later mutation
an explicit gate failure. A session counts toward admission only when it references the
current readiness-manifest digest and records passing audit integrity, an exact dated ES
contract, and redacted capability-report IDs.

## Source degradation drill

Inject each condition independently and verify a fail-closed result:

- stale or delayed market timestamp;
- crossed/empty quote or insufficient displayed size;
- mismatched/continuous ES contract;
- missing SPX, VIX/VIX1D, or SPXW chain entitlement;
- contradictory dual-source values;
- missing event feed, unconfirmed shock, or unstable cross-asset response;
- API restart and alert-stream reconnect.

Expected behavior: data quality becomes Amber/Red, Risk Lock remains engaged where
applicable, alert delivery is audited, and no precise executable recommendation is
shown.

## Local audit backup and recovery

The Docker `marketpilot-audit` volume contains derived decision and operator-audit
metadata. Raw licensed provider payloads are deliberately excluded.

Before backup, stop writes or use SQLite's online backup mechanism. Preserve the DB,
WAL, and SHM files as one consistent set. After restore, verify schema version, run an
integrity check, and replay known manifests before resuming shadow observation.

Run the repository check against the restored file:

```bash
marketpilot audit-check --database data/audit/marketpilot.sqlite3
```

Never repair corruption by editing append-only rows. Restore a verified backup and open
an incident instead.

## Model rollback

1. Keep the current session champion frozen.
2. Create a rollback approval tied to the current source version, target version,
   evidence hash, approver, timestamp, and note.
3. Exercise rollback only in `SCENARIO + LOCAL` until production approval infrastructure
   and validation evidence are authorized.
4. Confirm lineage and audit events; rerun deterministic replay fixtures.

## Incident closeout

Record detection time, first market reaction time, related event evidence, affected
snapshots/decisions/alerts, operator feedback, source recovery evidence, and the exact
rerun or rollback performed. Retain the reviewed shock as a reusable point-in-time
sample. Never include credentials, account identifiers, or raw licensed payloads in the
incident record.
