# Collector orchestration safety harness

`POST /v1/scenario/collector/run` is a deterministic, provider-neutral harness for
testing market-data collector behavior. It accepts only `SCENARIO` or `LOCAL` input,
performs no network calls, reads no provider credentials, and has no order path. Every
response fixes `execution_enabled=false` and `action=NO_TRADE`.

The harness models:

- connection, disconnection, exponential backoff, reconnect budgets, and `Retry-After`;
- permanent fail-closed halts after schema drift or an exhausted reconnect budget;
- duplicate event IDs, duplicate observations, event-time regression, out-of-order
  observations, allowed lateness, and a monotonic source-time watermark;
- collector freshness, entitlement/delay/quote quality, dual-source conflict, and
  capability readiness;
- the verified-range US equity calendar, holidays/half-days, the Globex maintenance
  window, and explicit dated ES contracts (continuous/main aliases are rejected).

Both the event accepted by the harness and the resulting collector state are emitted as
immutable point-in-time records with canonical content hashes. Re-running the same
scenario yields the same record IDs. After a disconnect or rate limit, the active quality
snapshot is discarded and both fresh sources must be observed again before
`permits_decision` can become true.

`permits_decision` means only that the simulated data path is internally healthy enough
to evaluate a decision. It never enables execution. Even a healthy scenario remains
unverified and returns `NO_TRADE`.

Faults are injected as typed events (`CONNECTION_LOST`, `RATE_LIMITED`, or
`SCHEMA_DRIFT`) in the request event sequence. `LIVE` is rejected at validation, as are
naive timestamps, future publication relative to first-seen time, unknown fields, and
implicit ES contracts.

Run the focused contract suite with:

```bash
.venv/bin/pytest tests/test_collector_orchestration.py -q
```
