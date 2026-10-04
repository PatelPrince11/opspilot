# OpsPilot NOTES

Running log of decisions, errors and fixes. Newest entries at the bottom.

## 2026-10-02: decisions after guide review

- **Langfuse Cloud Hobby, not self-hosted.** v3 self-hosting needs ClickHouse, Redis and S3; too heavy for compose and EC2. Added `TRACING_ENABLED` / `TRACING_SAMPLE_RATE` so bulk eval runs don't burn the monthly unit quota.
- **Split 100 dev / 100 test** (fixed seed). 50 dev was too small: one question is 2 points and per-tag slices were ~15. "Only claim large gaps" applies to the ablation too.
- **EC2: 4 GB first**, CPU-only torch, slim image. Measure memory in Phase 5, size up only if needed.
- **Injection set split 15 tune / 15 held-out**; report both. Defenses are tuned on the tune half only. Payloads live in a separate corpus flagged out of the retrieval eval.
- **Reproducibility:** commit the final-run response cache. "Reproduces" = retrieval metrics exactly, generation metrics within a stated tolerance.
- **Logs:** synthetic log events per incident, consistent with the services table; LogHub is background noise only. LogHub alone would not contain the demo scenario.
- **Full-text search uses the `simple` config** so error codes and hostnames are not stemmed or dropped. Tests with `ECONNREFUSED`, `503` and hostnames.
- **`/investigate` gets API-key auth and rate limiting** in Phase 5 (public endpoint spends free-tier keys and credits).
- **Recall@k / MRR at doc level:** chunks deduped to docs by each doc's best-ranked chunk.
- **Citation accuracy reported as two numbers:** (a) deterministic, cited id was retrieved; (b) judged, evidence supports the claim.
- **Verify node gets a config flag to disable it,** for the with/without comparison.
- **Attack success:** canary string or forbidden claim per case, fixed before any run.
- **Judge validation:** 30 dev answers, hand-labeled by Prince at the end of Phase 3.
- **Synthetic circularity:** questions paraphrase, never copy. Prince writes 20% (40) of the questions.
- **Embedder and reranker are local and outside the `llm/client.py` rule.** Clarified in CLAUDE.md.
- **`make eval` is a stub in Phase 1** (prints "not implemented", exits 0).

## 2026-10-03: Phase 1 step 1, scaffold

**Built:** docker-compose (Postgres 16 + pgvector, FastAPI app; no Langfuse container), `config.py` (pydantic-settings), `GET /health`, pytest + ruff, Makefile (`up`, `down`, `logs`, `test`, `lint`, `fmt`, `ingest` stub, `eval` stub), Dockerfile, README stub.

**Decisions**
- **uv** for deps, `uv.lock` committed, Python pinned to 3.11 (`.python-version`) so host and container match. Installed uv via Homebrew (was missing).
- **`config.py` at repo root**: api/, rag/, agent/ and eval/ all import it.
- **Tracing is opt-in:** `tracing_enabled` defaults to False; only `TRACING_ENABLED=true` turns it on. `.env.example` ships `false`. `TRACING_SAMPLE_RATE` validated to 0.0–1.0.
- **All published ports are loopback-only** (`127.0.0.1:`). DB port is for host-side psql/tests and is dropped on EC2.
- **Host ports are configurable, default `DB_HOST_PORT=5433`, `APP_HOST_PORT=8001`.** Container ports stay 5432/8000, so `DATABASE_URL` inside compose is still `db:5432`.
- **`/health` is liveness only** and returns `{"status":"ok","service":"opspilot"}`. DB readiness comes from the compose `pg_isready` healthcheck + `depends_on: service_healthy`.
- `extra_hosts: host.docker.internal:host-gateway` so Ollama-on-host works on Linux/EC2 too.
- Secrets reach the container at runtime via `env_file`; `.dockerignore` excludes `.env`. Verified `.env` is absent from the image.
- Langfuse SDK not installed yet; it arrives with `llm/client.py` in Phase 3.

**Errors and fixes**
- *Port clash.* First `make up` failed: `Bind for 0.0.0.0:5432 failed: port is already allocated`. Root cause: the FraudGraph compose stack was running and had published host ports 5432 and 8000. Worse, `curl :8000/health` returned `{"status":"ok"}` from **FraudGraph's** API: a false-positive health check. Fix: configurable host ports (5433/8001), and `/health` now includes `"service":"opspilot"`, which `make up` greps for, so another app on the port can't pass the check.
- *Deprecation warning.* Starlette's TestClient warns that `httpx` is deprecated in favor of `httpx2`. I first swapped to `httpx2`, then reverted: Starlette still falls back to `httpx` with only a warning, all tests pass on it, and `httpx2` was 5 months old (first release 2026-05-11, maintained by Pydantic). Kept `httpx` and silenced that one message with a `filterwarnings` entry in `pyproject.toml`. Revisit if Starlette drops the fallback.

**Verified:** 6 tests pass, ruff clean; both containers healthy; app reaches Postgres at `db:5432` (and `localhost:5432` from inside the app container is refused, as expected); pgvector 0.8.7 available (extension created in step 2 migrations); `make ingest` / `make eval` stubs exit 0.

**Gotcha for later:** `.env` has `DATABASE_URL=...@db:5432`. Host-side scripts that read `.env` need `localhost:5433` instead; handle this when `make ingest` is built (step 5).

## 2026-10-03: Phase 1 step 2, schema and migrations

**Built:** Alembic migrations in `db/migrations/` (`alembic.ini` at root), revision `0001`: pgvector extension, the 8 guide tables, HNSW index on `chunks.embedding vector(384)` (cosine ops, m=16, ef_construction=64), generated `tsv` column with a GIN index, `corpus` flag (main/injection). One-shot `migrate` compose service; `make migrate`, `make migrate-down`, `make psql`.

**Decisions**
- **Alembic, hand-written SQL, no autogenerate.** No ORM models to diff against, and autogenerate handles `vector`, generated tsvector columns and HNSW opclasses badly. Driver is psycopg 3 via `postgresql+psycopg://`, rewritten in `env.py`; URL comes from `config.py`. Readable revision ids (`0001`).
- **Migrations run in a one-shot `migrate` container** (same image as app). `app` depends on it with `service_completed_successfully`, so the app never starts on a half-migrated schema. Not in the app's CMD: that would restart-loop the API on a bad migration.
- **`make up` fails fast:** if migrate fails it prints `docker compose logs migrate` and exits non-zero, skipping the /health wait; it also checks migrate's exit code explicitly.
- **Host side:** Makefile builds `HOST_DATABASE_URL` (localhost:`DB_HOST_PORT`) from `.env` and passes it as `DATABASE_URL`; real env vars beat `.env` in pydantic-settings (tested). Settles the host-side URL gotcha from step 1; `make ingest` can reuse it.
- **text + CHECK, not ENUM** (enum values can't be removed). CHECKs on status, severity, level, doc_type, corpus, split, eval_type.
- **Stable text ids** for documents and incidents (`RB-012`, `INC-0042`): eval labels and citations reference them across re-ingests.
- **`corpus` on `log_events` as well as `documents`:** injection payloads also live in log messages, which the generation eval reads through `query_logs`.
- `log_events.service` has no FK (LogHub noise services aren't in `services`). `tool_calls.investigation_id` has no FK until Phase 3 adds `investigations` (migration 0002). `eval_results.metrics` is jsonb because metric sets differ per eval type.
- **Schema tests use a throwaway `opspilot_test` database** (dropped and recreated per run), never the dev DB. If Postgres is unreachable they skip with a visible reason (`-rs` in addopts).

**Errors and fixes**
- *Partial hostnames not findable.* The test querying `payments-db-01` against a chunk containing `payments-db-01.prod.internal` failed (0 matches). Root cause: Postgres's parser tokenizes the two sides differently. In the document the full name matches the `host` pattern and is stored as one token with no sub-parts; the query `payments-db-01` has no dot, so it's parsed as a hyphenated word: `'payments-db' & 'payments' & 'db' & '01'`, none of which is in the document. Probes also showed `prod.internal` and `db-01` (parsed as `'db' & '-01'`) missed. Not a `simple`-config problem. Fix: two immutable SQL functions in 0001. `opspilot_tsvector(text)` keeps the original tokens and adds a copy split on `. _ / : -`; the generated `tsv` uses it. `opspilot_tsquery(text)` applies the same split to queries. **Every keyword search must use `opspilot_tsquery`**; a raw `plainto_tsquery` misses partial hostnames again. Test now checks 6 positives and 3 negatives (`payments-db-02`, `orders-db-01`, `ETIMEDOUT`) and that `tsv` is built with the function. Trade-offs: terms are stored twice (shifts `ts_rank_cd` scores; Phase 2 ablation measures ranking), and `&` ignores order (Phase 2 can use phrase queries if the eval shows false hits). Edited 0001 in place since it was uncommitted and only applied locally; re-applied with `make migrate-down && make migrate`.
- *Verbose skip reasons.* psycopg's multi-host error printed every attempt; skip reason now uses the first line.

**Verified:** ruff clean; 19 tests pass with the stack up (12 schema tests incl. downgrade-to-base round trip, 384-dim enforcement, HNSW and GIN used in EXPLAIN, CHECKs, cascade); with the stack down, 7 pass and 12 skip with a reason (19 collected both ways). `make up`: migrate `Exited (0)`, app healthy. Throwaway `0002` with `SELECT 1/0`: `make up` printed migrate logs, exited 2, app stayed `Created`; DB still at `0001` (transactional DDL rolled back). `make migrate` from the Mac on 5433 reports head.

## Status

- Phase 0 accounts (AWS, Groq, Gemini, Langfuse Cloud, Anthropic credits, GitHub): **not done**. Prince is doing these in parallel. Repo files only until then.

## Unverified claims (check in Phase 0, record results here)

- AWS: Paid plan vs. Free plan behavior (account closing after 6 months), the $200 credits, the credit-unlock activities.
- Groq: current model list, tool-calling support, free-tier limits.
- Gemini Flash: free-tier limits.
- Langfuse Hobby: monthly unit quota.
