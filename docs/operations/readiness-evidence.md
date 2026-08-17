# External readiness and shadow evidence

MarketPilot treats external readiness as evidence, not configuration. A successful SDK
request or a locally simulated scenario cannot prove account entitlement, licensed-data
rights, point-in-time semantics, production controls, or an untouched holdout review.

The readiness gate is provider-neutral and intentionally narrower than production
authorization. Even when all evidence passes, its output remains:

- `production_ready=false`;
- `execution_enabled=false`;
- `action=NO_TRADE`;
- `manual_webull_execution_only=true`.

Passing this gate only means the recorded evidence is sufficient to admit an authorized,
read-only shadow workflow. It never creates permission to submit an order.

## Create the fail-closed manifest

```bash
marketpilot readiness-template \
  --output data/readiness/readiness-manifest.json \
  --environment production
```

The command deliberately exits with status 2 because every generated requirement is
`UNVERIFIED`. It refuses to overwrite an existing file. The manifest is owner-readable
only and contains metadata, never credentials, account identifiers, quotes, or licensed
payloads.

An authorized reviewer may change a requirement to `VERIFIED` only after recording all
of the following redacted metadata:

- a non-local authority class and issuer;
- UTC observation and expiry timestamps;
- a `sha256:<64 lowercase hex>` digest of the retained evidence artifact;
- a review identifier and exact scope;
- confirmation that secrets and licensed raw data were redacted.

`LOCAL_SIMULATION` is rejected for `VERIFIED` evidence. Duplicate requirements, naive
timestamps, missing reviews, expired evidence, unexpected fields, and embedded raw
payload flags all fail closed.

## Record a shadow session

Prepare a redacted JSON session summary containing the manifest digest, exact dated ES
contract, redacted capability-report IDs, deployed versions, decision counts, audit
integrity outcome, and drill outcomes. Every decision must be accounted for as either
`NO_TRADE` or `WAIT`; `execution_enabled` and raw-payload inclusion are constrained to
false, while automated orders are constrained to zero.

```bash
marketpilot shadow-record \
  --ledger data/readiness/shadow-sessions.jsonl \
  --session-file /secure/local/path/session-summary.json
```

The ledger uses monotonic sequence numbers, unique session IDs, a previous-entry hash,
and a canonical entry hash. Each session also links a redacted audit-export digest and
operator review ID; passing degradation or recovery drills require their own evidence
digest. The New York trading date is derived from the recorded start timestamp, rather
than trusted as an unrelated label. A session may count only after the current manifest
was generated and after the session has actually ended at the gate's UTC evaluation
time; future-dated sessions produce an explicit blocker. Appends are locked, forced to
owner-only `0600`, and flushed to disk. Any edited row, broken link, duplicate session,
invalid count,
continuous ES alias, or unexpected secret field makes the ledger invalid; the tool
refuses to append after corruption.

The hash chain is tamper-evident, not WORM storage. Retain each reported
`ledger_head_sha256` in an independently controlled audit system or immutable object
store. Production retention and access controls remain a separate external gate.

## Evaluate admission

```bash
marketpilot readiness-check \
  --manifest data/readiness/readiness-manifest.json \
  --shadow-ledger data/readiness/shadow-sessions.jsonl \
  --code-version "${MARKETPILOT_CODE_VERSION}" \
  --minimum-sessions 5 \
  --minimum-trading-dates 3
```

The default gate requires every external requirement to be current and verified, five
qualifying sessions across at least three trading dates, one source-degradation drill,
and one recovery drill. A session qualifies only when it references the current manifest
digest, has a passing audit-integrity result, names an explicit dated ES contract, and
records capability-report IDs. Its `code_version` must exactly match the evaluated
runtime artifact identity; sessions from another deployment do not qualify. The command
exits 2 while blocked.

The same fail-closed result is available from:

```text
GET /v1/readiness/shadow-admission
```

A missing or corrupt manifest/ledger returns HTTP 200 with `status=NOT_CONFIGURED`,
`NO_TRADE`, and an explicit blocker. HTTP availability must never be confused with
readiness. The endpoint exposes only derived status, requirement identifiers, manifest
and ledger digests, and counts; it never returns evidence issuer or scope metadata.
