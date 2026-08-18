# Data capability probe

Complete this before implementing provider-specific production mappings. Store only
redacted samples and metadata in Git; raw licensed payloads belong in the configured raw
landing store.

| Probe | Required evidence | Pass criteria | Status |
|---|---|---|---|
| Explicit ES contract | Ticker/expiry, bid/ask/size, exchange timestamp | No continuous-main substitution; timestamp semantics verified | Automated; awaiting account run |
| SPX index | Level, source timestamp, delayed flag, entitlement | Real-time and clearly identified | Not offered by Webull OpenAPI (SDK-surface finding); independent license required |
| VIX/VIX1D | Level, source timestamp, session | Freshness and session verified | Not offered by Webull OpenAPI (SDK-surface finding); independent license required |
| Same-day SPXW chain | Expiry, strike, C/P, NBBO/size, IV/Greeks, quote age | Coverage around center and executable timestamps | Automated; awaiting account run |
| Historical SPXW | Expired series and minute NBBO | Point-in-time fields and license confirmed | Partially automated (30 m1 bars, one exact symbol); depth and license remain external |
| Account entitlement | Market-data permission response | Display/non-display scope recorded | Schema automated via `/app/subscriptions/list`; scope mapping remains manual |

For each probe, record UTC execution time, account type (redacted), SDK/API version,
request semantics, response-field dictionary, measured P50/P95/P99 latency, delayed-data
behavior, reconnect behavior, and a Green/Amber/Red conclusion.

## Webull probe command

Run `marketpilot probe-webull` after setting local `WEBULL_APP_KEY` and
`WEBULL_APP_SECRET`. `WEBULL_ES_CONTRACT` must name a dated contract; `ESmain`, `ES`,
and `/ES` are rejected before any provider call. Exact SPXW quote/history probes also
require `WEBULL_SPXW_OPTION_SYMBOL`; discovery can be narrowed with
`WEBULL_SPXW_EXPIRATION`.

The persisted report contains only status, HTTP status, latency summaries, and observed
JSON field paths. Raw values and provider exception messages are intentionally omitted.
One request currently produces a one-sample P50/P95/P99 baseline; production readiness
requires scheduled multi-sample measurements, reconnect tests, entitlement evidence,
and timestamp-semantic review.

`quality=GREEN` in this report means only that every configured probe call returned a
non-empty success response. The report remains `verification_status=SCHEMA_ONLY` and
`production_ready=false`; MarketPilot therefore downgrades it at the live decision gate
and keeps `NO_TRADE`. Production readiness cannot be inferred from this automated probe.

## Access token activation

The SDK creates an access token during client initialization and polls until it
becomes `NORMAL`. A freshly issued or rotated app credential commonly starts as
`PENDING`; the probe bounds this wait (`WEBULL_TOKEN_CHECK_SECONDS`, default 65)
instead of blocking for the SDK's 300-second default, then records the resulting
downstream `401` responses as ordinary redacted failures.

If the report shows a PENDING-then-401 pattern, check the developer portal before
re-running: the app is activated/approved, any IP allowlist includes the probing
host, and account verification is complete. The SDK caches the token on disk; the
gateway pins that cache to the ignored `data/webull-token/` directory so the
credential file never lands in the repository root.

## Coverage findings (report version 2)

Since report version `2`, each run that reaches the SDK also records
`coverage_findings`: evidence-bounded conclusions for requirements a probe cannot
pass or fail.

- `SPX_INDEX_COVERAGE` and `VIX_VIX1D_COVERAGE` inspect the installed SDK surface.
  `webull-openapi-python-sdk` 2.0.14 exposes futures, option, stock, and crypto
  market-data modules but no index module, so the conclusion is `NOT_OFFERED` and the
  required action is licensing an independent point-in-time source. If a future SDK
  adds an index module, the conclusion becomes `UNVERIFIED` until probes are wired —
  never an automatic pass.
- `EXPIRED_SPXW_NBBO_DEPTH` stays `UNVERIFIED`: the automated history probe samples 30
  m1 bars for one exact symbol and cannot exercise expired-series depth or license
  scope.
- `MARKET_DATA_ENTITLEMENT_SCOPE` stays `UNVERIFIED`: the `account_subscriptions` probe
  records the response schema only; mapping entries to display/non-display scope is a
  manual step recorded in the readiness manifest.

A finding is a recorded conclusion, not an assumption: unmet requirements must appear
here or in `unverified_requirements`, never only in someone's head.

## Applying for Webull OpenAPI access

The probe needs an approved Webull OpenAPI account before it can produce account
evidence. When applying, request or confirm:

1. OpenAPI credentials (`WEBULL_APP_KEY` / `WEBULL_APP_SECRET`) for the `us` region.
2. US futures market-data access, so the explicit ES contract probes can pass.
3. US options market-data access, so SPXW discovery, snapshot, and history probes can
   pass.
4. Whatever subscription tier makes `/app/subscriptions/list` return the account's
   market-data permissions, so entitlement scope can be mapped.

SPX and VIX/VIX1D index feeds are not part of the SDK surface, so independent
licensing (Databento, Massive, or equivalent) is required regardless of the Webull
outcome. Keep credentials in a local `.env` or secret manager; never commit them.
