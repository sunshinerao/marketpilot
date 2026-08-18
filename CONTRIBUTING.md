# Contributing to MarketPilot

This document is the collaboration contract for everyone who works on this
repository — human developers and AI coding assistants alike. It exists so that
any contributor can pick up mid-stream without an oral handoff. Read it before
your first commit; follow it for every commit.

The repository voice is deliberate: docs state invariants plainly, roadmaps mark
unfinished work honestly, and code fails closed. Keep that voice.

## Non-negotiable safety invariants

These properties define the product. A change that weakens any of them is a
defect, regardless of test coverage.

- There is no order-submission interface. Do not add one. Automated execution is
  outside the authorization boundary (see `docs/development/roadmap.md`).
- `NO_TRADE` is a first-class result. Never convert a failed gate into an
  exception, a fabricated value, or a silent pass.
- `LIVE` decisions derive their timestamp, session identity, gates, and values
  only from server-owned state. Never trust a client-supplied LIVE timestamp or
  session identifier.
- A governance backend outage or champion mismatch freezes decisions. Never fall
  back to an arbitrary loaded model.
- Provider fields are interpreted only after capability probing has verified
  them against a real account response. Schema-observed is not verified.
- Secrets, tokens, account identifiers, and raw licensed market data are never
  committed. `.env` stays local; `.env.example` documents names only.
- Append-only audit stores stay append-only. Do not add update/delete paths or
  weaken the denial triggers.

When you touch code near these invariants, say so in the commit message body.

## Setup and quality gate

Requires Python 3.12 and Node.js 20 or newer.

```bash
make install   # venv + editable install with dev extras + web dependencies
make check     # ruff + mypy --strict + pytest (>=90% coverage) + web build
```

`make check` must pass before every commit. No exceptions for "docs-only"
changes that touch files referenced by checks. If the gate cannot run in your
environment, say so in the commit message and ask for review instead of
skipping silently. The same gate runs in GitHub Actions
(`.github/workflows/quality-gate.yml`) on every push and pull request to
`main`; a red CI run blocks merge regardless of who authored the change.

## Commit discipline

- Small, logical commits: one concern per commit, in dependency order
  (build → domain → validation → persistence → services → adapters → web → docs).
- Conventional Commit subjects (`feat:`, `fix:`, `build:`, `docs:`, `chore:`,
  `refactor:`, `test:`), imperative mood, no trailing period.
- The body explains intent and safety impact, not a diff summary. A reader
  running `git log` should understand the project history without opening diffs.
- Never commit generated artifacts: caches, coverage output, screenshots,
  local databases, probe reports. If evidence matters, reference where it lives
  in the commit message or docs instead.
- Push at the end of every work session. Unpushed work is unfinished work.

## Documentation obligations

Documentation is part of the deliverable, not an afterthought. When behavior
changes, update in the same commit or the immediately following one:

- `README.md` — user-facing contract: endpoints, commands, setup, boundaries.
- `docs/development/roadmap.md` — flip checkboxes only when the exit-gate
  evidence exists; never mark external gates done from code alone.
- `docs/development/safety-mvp-acceptance.md` — delivered-capability list and
  acceptance walkthrough.
- `docs/adr/` — a new ADR when a boundary or invariant decision changes;
  never edit an accepted ADR's decision retroactively.
- `docs/glossary.md` — new domain terms, defined once, used consistently.

Docs and code must not drift. If you find a discrepancy while working, fix it
in a dedicated `docs:` commit before continuing feature work.

## Handoff contract

Every work session — human or AI — ends in a state the next contributor can
resume from without asking questions:

1. `git status` clean; everything either committed and pushed or deliberately
   ignored via `.gitignore`.
2. `make check` green on the pushed HEAD.
3. Progress recorded where the next contributor looks first: roadmap checkboxes,
   acceptance notes, or an ADR — not only in commit messages.
4. Open questions and known risks written down (roadmap "exit gate" lines or a
   dated note in `docs/development/`), never left as tribal knowledge.

## What code cannot self-certify

Some gates require external evidence: a real provider account, licensed
point-in-time data, production infrastructure, untouched holdout validation,
and a completed shadow period. They are tracked in
`docs/development/safety-mvp-acceptance.md` ("Gates that code cannot
self-certify") and `docs/operations/readiness-evidence.md`. No commit may claim
them. Until they pass, `execution_enabled` remains `false` and all real
execution remains a separate manual action.
