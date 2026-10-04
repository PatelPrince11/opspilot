.PHONY: up down logs test lint fmt ingest eval

-include .env
APP_HOST_PORT ?= 8001

up:
	docker compose up -d --build
	@echo "waiting for /health on 127.0.0.1:$(APP_HOST_PORT)..."
	@for i in $$(seq 1 30); do \
		curl -fsS http://127.0.0.1:$(APP_HOST_PORT)/health 2>/dev/null | grep -q '"service":"opspilot"' \
			&& echo "app healthy" && exit 0; \
		sleep 1; \
	done; echo "app did not become healthy; run 'make logs'"; exit 1

down:
	docker compose down

logs:
	docker compose logs -f

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

ingest:
	@echo "ingest: not implemented (Phase 1 step 5)"

eval:
	@echo "eval: not implemented (Phase 2)"
