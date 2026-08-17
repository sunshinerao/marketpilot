# MarketPilot

MarketPilot is a cross-market decision intelligence platform. The first model plugin is
`strikepilot_spxw_0dte_ic`, which supports SPXW 0DTE iron-condor decisions without
placing orders.

This repository started at Phase 0 and now includes a local safety MVP: stable domain
contracts, versioned rules, model registration, deterministic point-in-time replay,
event Risk Lock, alert feedback, validation/governance contracts, and a capability-probe
workflow. Provider-specific production logic must not be enabled until its fields,
licenses, and entitlements have been verified against a real account response.

## Repository layout

```text
apps/web/                    Next.js dashboard shell
config/                      Versioned thresholds and rule configuration
docs/adr/                    Architecture decision records
docs/development/            Capability probe and delivery guidance
src/marketpilot/domain/      Cross-asset domain objects
src/marketpilot/adapters/    Provider-neutral adapter contracts
src/marketpilot/features/    Reproducible feature calculations
src/marketpilot/models/      Plugin contract, registry, model packages
src/marketpilot/decision/    Common RiskPilot gates and reason codes
src/marketpilot/services/    FastAPI boundary
tests/                       Unit and contract tests
```

## Local setup

Requires Python 3.12 and Node.js 20 or newer.

Production runtime dependencies are pinned in `constraints/production.txt`. Updating
that file requires rerunning the full quality gate plus the PostgreSQL and browser
acceptance flows; the API image always installs against those constraints.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
pytest
uvicorn marketpilot.services.api:app --reload
```

In another terminal:

```bash
cd apps/web
pnpm install
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000). The dashboard renders API status
server-side; when the API is unavailable it fails closed and displays `NO TRADE`.
The workbench provides three explicit scenarios: unverified Live, CPI Risk Lock, and an
event-cleared review-only strike map. Every scenario remains non-executable.

### Exercise the decision API

The default request intentionally returns `NO_TRADE` because no live capabilities are
verified. `LIVE` accepts neither `as_of` nor a session identifier: both the decision
timestamp and `LIVE:XNYS:<New-York-date>` governance session are derived by the server.

```bash
curl -sS http://localhost:8000/v1/decision/run \
  -H 'content-type: application/json' \
  -d '{}'
```

Run the non-executable design-document example (the result remains `WAIT`, not `ENTER`,
because the baseline is not calibrated):

```bash
curl -sS http://localhost:8000/v1/decision/run \
  -H 'content-type: application/json' \
  -d '{
    "run_mode":"SCENARIO",
    "scenario_session_id":"manual-review-2026-08-17-a",
    "as_of":"2026-08-17T13:45:00Z",
    "values":{"center":7812.4,"up_tail":28.6,"down_tail":34.2,"joint_buffer":3.5},
    "gates":{"data_quality":"GREEN","event_cleared":true,"option_chain_usable":true,"edge_ok":true}
  }'
```

Every `SCENARIO` decision requires an explicit `scenario_session_id`. It is stored as a
separate `SCENARIO:<id>` governance namespace and cannot be used to pre-freeze or alter a
LIVE session.

Run the complete container stack:

```bash
docker compose up --build
```

### Probe Webull capabilities

MarketPilot uses Webull only after the configured account proves each required
capability. Keep credentials in a local `.env` or secret manager, then provide an
explicit dated ES contract and, when available, one exact SPXW option symbol:

```bash
export WEBULL_APP_KEY='<local-secret>'
export WEBULL_APP_SECRET='<local-secret>'
export WEBULL_ES_CONTRACT='ESU6'
export WEBULL_SPXW_EXPIRATION='2026-08-17'
export WEBULL_SPXW_OPTION_SYMBOL='SPXW...'
marketpilot probe-webull
```

The command writes a redacted, versioned report under the ignored
`data/capability-probes/webull/` directory and exits non-zero until every configured
probe passes. It never stores credentials, account identifiers, or quote values. Read
the latest result at `GET /v1/providers/webull/capabilities`; market state remains
non-executable even when the provider probe is Green.

Useful read-only and local-operations endpoints:

- `GET /v1/overview` — server-derived fail-closed operating state.
- `GET /v1/readiness/shadow-admission` — external-evidence and hash-chained shadow
  admission status; always non-executable and fail-closed when evidence is absent.
- `GET /v1/demo/scenarios` — deterministic point-in-time Risk Lock fixtures.
- `POST /v1/events/assess` — explicit `SCENARIO` event assessment only.
- `GET /v1/alerts` and `POST /v1/alerts/{id}/feedback` — local alert/feedback workflow.
- `GET /v1/alerts/stream` — resumable local SSE stream with `Last-Event-ID` and
  append-only delivery-attempt audit.
- `POST /v1/attribution/signals` — explicit `SCENARIO + LOCAL` reverse-attribution
  workflow and counterfactual replay link.
- `POST /v1/scenario/session-quality/*` — fail-closed calendar, Globex, and dual-source
  quote-quality evaluations.
- `POST /v1/scenario/economics/assess` — conservative NBBO, fees, slippage, EV, CVaR,
  and risk-budget assessment; an eligible sub-gate returns only `WAIT`.
- `POST /v1/scenario/collector/run` — deterministic reconnect, rate-limit, schema-drift,
  watermark, freshness, and PIT collector fault injection.
- `POST /v1/scenario/alert-delivery/run` — no-network outbox simulation with signatures,
  dedupe, retry, dead-letter, acknowledgment, and escalation.
- `GET /v1/history/decisions`, `GET /v1/history/replay-manifests`, and
  `GET /v1/audit/integrity` — durable history and append-only integrity evidence.
- `GET /v1/validation/promotion-criteria` and `POST /v1/validation/promotion-gate` —
  pre-registered, local-only holdout criteria and tamper-evident evaluation.
- `GET /v1/governance/models/{model_id}/versions` and related local governance routes —
  local challenger registration, frozen champion inspection, explicit approval, session
  freeze, and rollback. A champion without a loaded artifact freezes decisions.
- `GET /docs` — generated API contract.

`/docs` is available only in the local development mode. Shared and production modes
require role-scoped bearer authentication for every `/v1` route, reserve model
governance writes for a separate reviewer credential, and do not register docs, ReDoc,
or OpenAPI endpoints. See
[`docs/operations/production-authentication.md`](docs/operations/production-authentication.md).

The default stack persists decisions, alerts/feedback, point-in-time manifests, SSE
delivery attempts, and attribution evidence in append-only SQLite. A PostgreSQL runtime
adapter covers the same core evidence plus durable governance. The local PostgreSQL
overlay, migration, encrypted raw-landing boundary, and recovery contracts are included;
production still requires TLS, separate roles, a secret manager, KMS/HSM encryption,
immutable object-store IAM/retention, and a verified backup/restore drill.

For the local PostgreSQL migration/adapter harness, follow
[`docs/operations/production-persistence.md`](docs/operations/production-persistence.md).
For external-evidence manifests, append-only shadow-session recording, and the
fail-closed admission check, follow
[`docs/operations/readiness-evidence.md`](docs/operations/readiness-evidence.md).

Verify the local audit database without modifying it:

```bash
marketpilot audit-check --database data/audit/marketpilot.sqlite3
```

The command fails closed when the database is missing, its SQLite integrity check
fails, the schema version is unexpected, or an append-only trigger is absent or does
not match the expected denial definition. Existing SQLite databases are checked through
a read-only connection before the writable runtime opens, so startup does not silently
repair and conceal a damaged audit boundary.

See [Safety MVP acceptance](docs/development/safety-mvp-acceptance.md) for the delivered
capabilities, test evidence, walkthrough, and remaining external gates.

## Safety boundary

- `No Trade` is a valid first-class result.
- The MVP is read-only decision support; it contains no order-submission interface.
- A Green decision requires explicit contracts, fresh source timestamps, verified
  entitlements, and an event-cleared state.
- Secrets, tokens, account identifiers, and raw licensed market data must not be
  committed.
- Compose publishes API, UI, and local PostgreSQL only on `127.0.0.1`. Any shared or
  production deployment must use protected auth mode plus TLS and an identity-aware
  ingress before exposing any `/v1` route.

## Contributing and handoff

All contributors — human or AI — follow the collaboration contract in
[CONTRIBUTING.md](CONTRIBUTING.md): safety invariants, the `make check` quality
gate, commit discipline, documentation obligations, and the end-of-session
handoff state.

## Git remote

The private upstream is [sunshinerao/marketpilot](https://github.com/sunshinerao/marketpilot)
on branch `main`.
