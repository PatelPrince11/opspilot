.PHONY: gen-eval judge-sheet judge-agreement demo up down logs test lint fmt gate results results-check migrate migrate-down psql corpus corpus-check selfcheck packet ingest eval import-human audit-pack import-audit

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

# Everything a commit must pass, as one bash script with -e and pipefail: the first failing step
# stops the gate, and a failure inside a pipe is not hidden by the command after it. (macOS
# make 3.81 has no .SHELLFLAGS, hence bash -c.) Ends by requiring a clean tree, so a corpus
# rebuild that changed a tracked file fails here. Never pipe this target's output.
gate:
	@bash -euo pipefail -c '\
		$(MAKE) -s corpus-check; \
		$(MAKE) -s test; \
		$(MAKE) -s lint; \
		uv run python eval/validate_questions.py --final; \
		$(MAKE) -s results-check; \
		if [ -n "$$(git status --porcelain)" ]; then \
			echo "gate: working tree not clean:"; git status --short; exit 1; fi; \
		echo "gate: OK"'

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
#   make audit-pack [FROM=N]          -> scratchpad/gemini_audit/corpus.md, batch_NN..08.md
#   make import-audit FILE="a.txt b.txt" -> eval/label_audit.md (files merged)
audit-pack:
	@uv run python eval/label_audit.py pack $(if $(FROM),--from $(FROM))

import-audit:
	@uv run python eval/label_audit.py import $(FILE)

# Host-side: loads data/processed into Postgres and embeds on the Mac's CPU.
ingest:
	@DATABASE_URL=$(HOST_DATABASE_URL) uv run python -m rag.ingestion

# docs/results.md, generated from eval_runs, the DB and repo files (never edited by hand).
# results-check (in the gate) fails when it is stale, e.g. after a new eval run.
results:
	@DATABASE_URL=$(HOST_DATABASE_URL) uv run python -m scripts.results

results-check:
	@DATABASE_URL=$(HOST_DATABASE_URL) uv run python -m scripts.results --check

# Retrieval eval on dev with the locked config; extra flags via ARGS, e.g.
#   make eval ARGS="--mode dense --no-bge-prefix". Test needs --split test --final-test (Phase 5).
eval:
	@DATABASE_URL=$(HOST_DATABASE_URL) uv run python -m eval.retrieval_eval $(ARGS)

# One demo investigation with a transcript (host-side; Ollama on the Mac is localhost):
#   make demo ARGS="--llm ollama:qwen2.5:7b --out scratchpad/phase3/demo_ollama.md"
demo:
	@DATABASE_URL=$(HOST_DATABASE_URL) OLLAMA_HOST=http://localhost:11434 uv run python -m scripts.demo $(ARGS)

# Generation eval on dev (eval/generation_eval.py): verify on then off, resumable, cached;
# writes eval/results/dev_generation_verify_{on,off}.json. Extra flags via ARGS, e.g.
#   make gen-eval ARGS="--config verify_on --concurrency 3 --max-spend 4.5"
gen-eval:
	@DATABASE_URL=$(HOST_DATABASE_URL) uv run python -m eval.generation_eval run $(ARGS)

# eval/judge_validation.md: 30 seeded, stratified verify-on answers for Prince to label.
judge-sheet:
	@uv run python -m eval.generation_eval sheet

# Percent agreement and Cohen's kappa, Prince's labels vs the judge's.
judge-agreement:
	@uv run python -m eval.generation_eval agreement
