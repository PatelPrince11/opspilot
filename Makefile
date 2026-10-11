.PHONY: up down logs test lint fmt migrate migrate-down psql corpus corpus-check selfcheck packet ingest eval import-human audit-pack import-audit

-include .env
APP_HOST_PORT ?= 8001
DB_HOST_PORT ?= 5433
POSTGRES_USER ?= opspilot
POSTGRES_PASSWORD ?= change-me
POSTGRES_DB ?= opspilot

# .env's DATABASE_URL (db:5432) only resolves inside Docker. Host-side commands get
# this one instead; a real env var beats .env in config.py. A password containing
# URL-special characters (@ : / #) would need percent-encoding here.
HOST_DATABASE_URL = postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@localhost:$(DB_HOST_PORT)/$(POSTGRES_DB)

up:
	@docker compose up -d --build || { \
		echo "docker compose up failed; migrate logs:"; docker compose logs migrate; exit 1; }
	@code=$$(docker compose ps -a migrate --format '{{.ExitCode}}'); \
	if [ "$$code" != "0" ]; then \
		echo "migrate exited with code $$code; logs:"; docker compose logs migrate; exit 1; fi
	@echo "migrations applied; waiting for /health on 127.0.0.1:$(APP_HOST_PORT)..."
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
	@DATABASE_URL=$(HOST_DATABASE_URL) uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

migrate:
	@DATABASE_URL=$(HOST_DATABASE_URL) uv run alembic upgrade head

migrate-down:
	@DATABASE_URL=$(HOST_DATABASE_URL) uv run alembic downgrade -1

psql:
	docker compose exec db psql -U $(POSTGRES_USER) -d $(POSTGRES_DB)

# Corpus build runs in the app container (data/ is bind-mounted). --build keeps the
# script current; the dependency layer stays cached.
corpus:
	docker compose run --rm --build corpus

corpus-check:
	docker compose run --rm --build corpus python -m scripts.build_corpus --check

# One draft write-up, every deterministic check:
#   make selfcheck ID=INC-NNNN [FILE=path] [DRAFTS=dir]
selfcheck:
	@uv run python -m scripts.corpus.selfcheck $(ID) $(if $(FILE),--file $(FILE)) $(if $(DRAFTS),--drafts $(DRAFTS))

# Writer packet for one row: make packet ID=INC-NNNN -> scratchpad/packets/INC-NNNN.md
packet:
	@uv run python -m scripts.corpus.packet $(ID)

# The 40 hand-written (or Gemini) questions, "N<tab>question" per line, appended unlabelled:
#   make import-human FILE=path AUTHOR=prince|gemini
import-human:
	@uv run python eval/validate_questions.py --import-human $(FILE) --author $(AUTHOR)

# Gemini label audit, pasted by hand into AI Studio (no LLM calls from code):
#   make audit-pack            -> scratchpad/gemini_audit/corpus.md, batch_01..08.md
#   make import-audit FILE=path -> eval/label_audit.md
audit-pack:
	@uv run python eval/label_audit.py pack

import-audit:
	@uv run python eval/label_audit.py import $(FILE)

ingest:
	@echo "ingest: not implemented (Phase 1 step 5)"

eval:
	@echo "eval: not implemented (Phase 2)"
