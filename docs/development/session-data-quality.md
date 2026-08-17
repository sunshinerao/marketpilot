# Session, contract, and quote-quality integration

This work package is provider-neutral and fail-closed. It does not map or infer any Webull
response field. A Webull field may enter these interfaces only after the account capability probe
has verified its meaning, entitlement, timestamp semantics, delay status, and availability.

## Trading calendars

- `EquityCalendarConfig` takes an explicit verified date range, holiday dates, and early-close
  times. Weekdays outside that range return `TradingDayStatus.UNVERIFIED` and do not permit
  trading.
- `USEquityCalendar.session(date)` supplies timezone-aware open, 09:29:59 cutoff, close, and
  anchor timestamps in `America/New_York`. The anchor is 16:00 on a regular day and the actual
  exchange close on an explicitly configured half-day.
- `GlobexSessionClock.state_at(datetime)` converts any aware instant to New York time and returns
  `OPEN`, `MAINTENANCE`, `CLOSED`, or `UNVERIFIED`. The daily maintenance interval is 17:00-18:00;
  the weekend closure is Friday 17:00 through Sunday 18:00.

Calendar dates must be populated from a maintained authoritative source. The module deliberately
ships without a guessed holiday list.

## ES contracts

Use `ExplicitESContract(symbol, expiry)` when binding a market-data observation to an ES contract.
It requires an `ES` quarterly month code and year, verifies the supplied expiry month and year,
and rejects generic, continuous, and `main` aliases. `WebullSettings.es_contract` reuses the same
symbol validator before any network call.

## Quote quality

Normalize provider-verified fields into `QuoteObservation`; required inputs are:

- source and canonical instrument identifiers;
- timezone-aware `source_ts`, `received_ts`, and per-field timestamps;
- an explicit delay flag and entitlement status;
- bid, ask, bid size, and ask size.

`QuoteQualityEvaluator(QualityPolicy).evaluate(observations, as_of=...)` returns a
`FeedQualityReport`. It derives `GREEN`, `AMBER`, or `RED` from field freshness, delivery latency,
entitlement, delay status, quote integrity, source count, instrument consistency, and cross-source
midpoint tolerance. A cross-source conflict is `RED` and freezes decisions. All `RED` results
freeze; `AMBER` does not freeze ingestion but still has `permits_decision == False`.

The decision runner can integrate without an API schema change by passing `report.status` into
`DecisionGateContext.data_quality`; persist `reasons`, `stale_fields`, `sources`, and `observed_at`
with the immutable input snapshot. Do not translate an unknown provider field into a guessed
`QuoteObservation` value.

## Scenario-only API router

`marketpilot.services.session_quality_router.create_session_quality_router()` exposes typed
scenario evaluations under `/v1/scenario/session-quality`. It does not mount itself into the main
application and does not accept `LIVE` as a run mode. Every successful response is explicitly
`UNVERIFIED`, with `execution_enabled` constrained to the literal value `false` and `action`
constrained to `NO_TRADE`. A single scenario sub-gate never emits a trade action.

Mount it only where the scenario surface is intended:

```python
from marketpilot.services.session_quality_router import create_session_quality_router

app.include_router(create_session_quality_router())
```

The endpoints are `POST /equity-session`, `POST /globex-session`, and `POST /quote-quality`
relative to that prefix. They are pure evaluations and do not write state or place orders.
