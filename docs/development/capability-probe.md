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

