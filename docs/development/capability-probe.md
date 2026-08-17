# Data capability probe

Complete this before implementing provider-specific production mappings. Store only
redacted samples and metadata in Git; raw licensed payloads belong in the configured raw
landing store.

| Probe | Required evidence | Pass criteria | Status |
|---|---|---|---|
| Explicit ES contract | Ticker/expiry, bid/ask/size, exchange timestamp | No continuous-main substitution; timestamp semantics verified | Not run |
| SPX index | Level, source timestamp, delayed flag, entitlement | Real-time and clearly identified | Not run |
| VIX/VIX1D | Level, source timestamp, session | Freshness and session verified | Not run |
| Same-day SPXW chain | Expiry, strike, C/P, NBBO/size, IV/Greeks, quote age | Coverage around center and executable timestamps | Not run |
| Historical SPXW | Expired series and minute NBBO | Point-in-time fields and license confirmed | Not run |
| Account entitlement | Market-data permission response | Display/non-display scope recorded | Not run |

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
