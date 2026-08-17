.PHONY: install test check dev-api dev-web build-web up down postgres-restore-drill postgres-restore-drill-plan

install:
	python3.12 -m venv .venv
	.venv/bin/python -m pip install -e '.[dev]'
	cd apps/web && pnpm install

test:
	.venv/bin/pytest --cov=marketpilot --cov-report=term-missing:skip-covered --cov-fail-under=90

check:
	.venv/bin/ruff check .
	.venv/bin/mypy
	.venv/bin/pytest --cov=marketpilot --cov-report=term-missing:skip-covered --cov-fail-under=90
	cd apps/web && CI=true pnpm build

dev-api:
	.venv/bin/uvicorn marketpilot.services.api:app --reload --port 8000

dev-web:
	cd apps/web && MARKETPILOT_API_URL=http://127.0.0.1:8000 pnpm dev

build-web:
	cd apps/web && pnpm build

up:
	docker compose up --build

down:
	docker compose down

postgres-restore-drill-plan:
	.venv/bin/python -m marketpilot.restore_drill --plan

postgres-restore-drill:
	.venv/bin/python -m marketpilot.restore_drill
