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
- *Deprecation warning.* Starlette's TestClient warned that `httpx` is deprecated in favor of `httpx2`; swapped the dev dependency.

**Verified:** 6 tests pass, ruff clean; both containers healthy; app reaches Postgres at `db:5432` (and `localhost:5432` from inside the app container is refused, as expected); pgvector 0.8.7 available (extension created in step 2 migrations); `make ingest` / `make eval` stubs exit 0.

**Gotcha for later:** `.env` has `DATABASE_URL=...@db:5432`. Host-side scripts that read `.env` need `localhost:5433` instead; handle this when `make ingest` is built (step 5).

## Status

- Phase 0 accounts (AWS, Groq, Gemini, Langfuse Cloud, Anthropic credits, GitHub): **not done**. Prince is doing these in parallel. Repo files only until then.

## Unverified claims (check in Phase 0, record results here)

- AWS: Paid plan vs. Free plan behavior (account closing after 6 months), the $200 credits, the credit-unlock activities.
- Groq: current model list, tool-calling support, free-tier limits.
- Gemini Flash: free-tier limits.
- Langfuse Hobby: monthly unit quota.
