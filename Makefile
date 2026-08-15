.PHONY: install test check dev-api dev-web build-web up down

install:
	python3.12 -m venv .venv
	.venv/bin/python -m pip install -e '.[dev]'
	cd apps/web && pnpm install

test:
	.venv/bin/pytest

check:
	.venv/bin/ruff check .
	.venv/bin/mypy
	.venv/bin/pytest
	cd apps/web && pnpm build

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

