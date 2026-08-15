# MarketPilot

MarketPilot is a cross-market decision intelligence platform. The first model plugin is
`strikepilot_spxw_0dte_ic`, which supports SPXW 0DTE iron-condor decisions without
placing orders.

This repository starts at Phase 0 of the technical design: stable domain contracts,
versioned rules, model registration, deterministic snapshots, safety gates, and a
capability-probe workflow. Provider-specific production logic must not be added until
its fields and entitlements have been verified against a real account response.

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

PostgreSQL is optional for the current in-memory skeleton:

```bash
docker compose up -d postgres
```

## Safety boundary

- `No Trade` is a valid first-class result.
- The MVP is read-only decision support; it contains no order-submission interface.
- A Green decision requires explicit contracts, fresh source timestamps, verified
  entitlements, and an event-cleared state.
- Secrets, tokens, account identifiers, and raw licensed market data must not be
  committed.

## Git remote

The local repository uses branch `main`. Connect a remote only after its URL and owner
are confirmed:

```bash
git remote add origin <repository-url>
git push -u origin main
```

