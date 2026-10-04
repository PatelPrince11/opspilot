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

## 2026-10-03: Phase 1 step 3, batch 0 (corpus infrastructure)

Plan for step 3, approved with review changes:
- batches: infra → runbooks ×3 → a 200-row incident skeleton (reviewed before any prose) → 10 calibration incidents → ~27 per batch
- eval-only fields live in a sidecar (`eval/corpus/`) that ingest never reads
- the open demo incident `INC-LIVE-001` is not one of the 200 and not a document

**Built:**
- `data/services.toml`: 24 services, 13 apps and 11 datastores, plus hosts, IPs, ports, statuses and an error-code catalog
- `scripts/build_corpus.py` with `scripts/corpus/{common,services,loghub}.py`
- a one-shot `corpus` compose service; `make corpus` and `make corpus-check`
- `data/processed/{services.json, MANIFEST.json}`
- `tests/test_corpus.py` (22 tests)

**Decisions**
- **The corpus build runs in the app container.** This settles the host-or-container question left open in step 1. It's a `corpus` service under `profiles: ["tools"]`, so `make up` never starts it, and it has no DB dependency because it writes files only (`make ingest` loads Postgres in step 5). `./data` is bind-mounted read-write so outputs land on the host. `./eval/corpus` is mounted read-only because the build only validates against the sidecar. `--build` on `docker compose run` means the image never runs a stale script; the deps layer stays cached.
- **Who writes the content:** Claude Code writes runbook and incident prose as files and Prince reviews them. The script only does deterministic work, and no code makes an LLM call.
- **No new dependencies:** TOML front matter (`tomllib`), `urllib`, `csv`.
- **Seed** `20261003`. One RNG per item (`sha256(f"{SEED}:{key}")`), so editing one incident never reshuffles another's logs. `hashlib` because `hash()` is salted per process.
- **MANIFEST hashes committed outputs only.** It records the LogHub commit and file pins but not LogHub-derived files.
- **LogHub: OpenStack 2k structured sample**, pinned to commit `dd61d09` (master on 2026-10-03), with sha256 checks on the CSV and the LICENSE. A mismatched download raises and writes nothing. A valid cached copy is reused; a corrupt one is re-downloaded. The fallback to synthetic noise wasn't needed: it worked on the first try, well inside the 1-hour time box.
- **LogHub license** ("LICENSE OF LOGHUB"): free for research or academic work, cite the ISSRE 2023 paper, and include the notice in all copies. So nothing LogHub-derived is committed: `data/raw/*` and `log_events.loghub.jsonl` are gitignored. Even the test fixture is **made up** in LogHub's column format rather than cut from the real file. The README carries the citation.
- **LogHub facts (measured):** 2,000 rows (1,969 INFO, 31 WARNING; `WARNING` → `WARN`), from three source logs (nova-api 1,060, nova-compute 933, nova-scheduler 7), all within 15 minutes on 2017-05-16. So rows are **rebased**: a seeded contiguous slice of 40 per incident window, order kept, `incident_id` NULL. Service names are `openstack-nova-*`, outside `services` (no FK on `log_events.service`). 0 placed so far because there are no incidents yet.
- **`--check`** builds in memory and compares to the committed bytes. It writes and downloads nothing, so it can run in CI later.

**Errors and fixes**
- *Fixture licensing.* I first cut the test fixture from the real CSV, then replaced it with made-up rows: committing even 5 real lines is a copy under the LogHub notice.
- *Lint:* one line over 100 characters in a test; fixed with `make fmt`.

**Verified:**
- `make corpus` in the container: 24 services, 2,000 LogHub lines parsed.
- A second run produced a byte-identical `MANIFEST.json`.
- `make corpus-check` passes. On a tampered `services.json` it exits 1 and names the file; after restoring, it exits 0.
- `make test`: 41 passed (22 new). `make lint` is clean.
- Downloaded hashes match the pins.

## 2026-10-03: Phase 1 step 3, batch 1 (runbooks RB-001 to RB-010)

**Built:** `data/runbooks/RB-001.md` … `RB-010.md` (high CPU ×3, memory ×3, disk ×3, pgx pool ×1); `scripts/corpus/runbooks.py` (load, validate, output); `data/processed/runbooks.jsonl` added to the committed outputs and MANIFEST; runbook tests in `tests/test_corpus.py`.

**Decisions**
- **Assumption A: standing decision for every later batch (runbooks and incidents).** It covers the fictional company only; OpsPilot's own infrastructure still has no Kubernetes.
  - Application services run on Kubernetes: one Deployment per service, named after the service, in a namespace named after the owning team without `team-` (`payments`, `commerce`, `catalog`, `risk`, `platform`, `identity`, `growth`). Actions use `kubectl -n <ns> ... deploy/<service>`.
  - Datastores (Postgres, Kafka, OpenSearch, Redis) run on VMs, reached by their `services.toml` hostnames (SSH, `systemctl`).
  - App hostnames (`orders-api-02.prod.internal`) are the stable instance names the logging agent stamps on log lines. Documents quote them in log and dashboard references; they never name a host that isn't in `services.toml` (no new names when scaling).
- **Front matter:** TOML between `+++` lines with `id, title, services, failure_types, owner, format, last_reviewed, synthetic`. `format` was added for the diversity test and is not an eval label.
- **`failure_types` holds 1 or 2 values** from the 10-type vocabulary. RB-002 is `high_cpu` + `api_latency`.
- **Validation against the catalog:**
  - every `*.prod.internal` hostname in a body must exist in `services.toml`
  - every token with an internal-code prefix (`PAYAPI-`, `LEDGER-`, …) must be a catalog code; prefixes come from the catalog, so `SHA-256` or `RB-010` don't trigger false hits
  - services must exist and the owner must own some service
- **Runbooks stand alone.** RB-010 doesn't explain how it differs from PgBouncer saturation or `53300` (RB-011/012); telling them apart is the retriever's job.
- **Demo-leak guard is scoped** to the demo's own combination (`payments-pgbouncer-01` + `ECONNREFUSED` + `6432` in one doc). Generic config-push text passes, so RB-027 won't be blocked. Batch 4's `demo_answer.toml` replaces it.
- **Diversity, measured on batch 1:**
  - max pairwise word-trigram Jaccard **0.0108** (RB-004 vs RB-005; 0.0123 before the second-review edits); threshold set to **0.03**, with room for batches 2–3 and their hard-negative pairs
  - word counts 189–1,072
  - 8 formats across 10 runbooks
  - no shared heading sequences, and none shared after the title either
  - headings inside code fences are ignored, because shell comments start with `#`

**Not yet verified against upstream docs:** the commands, config keys and behaviors in the batch-1 uncertainty list (PgBouncer, Postgres, Kubernetes, JVM, Node, OpenSearch, Kafka, Linux). Prince is checking them; record the corrections here.

**Fixes after first review**
- *Wrong SQLSTATE in RB-007.* RB-007 said clients get `57P01` after the PANIC. Root cause: I mixed up two codes. When a backend PANICs, the postmaster kills the other sessions with `57P02` (crash_shutdown); `57P01` (admin_shutdown) only comes from an administrative shutdown of a running server. I had left `57P02` out because it wasn't in the catalog, which meant fitting the text to the catalog instead of to Postgres. Fix: added `57P02` to `services.toml`, so the catalog now has **32 codes: 17 real-world, 15 fictional internal**, and a test asserts those counts. RB-007 now says `57P02` after a PANIC and `57P01` only on an administrative shutdown.
- *How payments-api talks to PgBouncer was unstated.* pgx v5's default exec mode uses protocol-level prepared statements, which break behind transaction pooling unless PgBouncer tracks them. Decision: **PgBouncer 1.22, `pool_mode = transaction`, `max_prepared_statements = 200`; pgx stays on its default `cache_statement` mode, no simple protocol.** This keeps prepared statements, is the current supported path, and needs no client-side workaround. Stated in RB-010 and in Fictional facts below.
- Only RB-007 and RB-010 changed; the other eight runbooks are untouched.

**Fixes after second review** (RB-002, 003, 004, 006, 007, 010 and the validator; the other runbooks untouched)
- *RB-002 arithmetic.* The old text had every period burning its quota in 12 ms, yet a 50% minute average. Those two can't both be true: throttling in every period means close to 100% average use. The real mechanism is burstiness. Some periods exhaust the quota and others sit nearly idle, so the average stays low while many periods are throttled. Now: 30% of periods throttled, the rest at about 20% of quota, giving roughly 45% average. The March 2025 incident uses 30% / 45%.
- *RB-010: `acquire_timeout` isn't a pgxpool setting* (`Acquire` waits on its context). It's now our app setting `DB_ACQUIRE_TIMEOUT=500ms`, applied as a context deadline. The `services.toml` catalog entry was fixed to match after Prince approved (third review, below).
- *RB-006:* `py-spy dump` doesn't document `--subprocesses`. It now dumps each worker PID (`pgrep -P 1`), or uses `py-spy top --subprocesses`. The tracemalloc URL is quoted, since an unquoted `?` is a glob in zsh.
- *RB-004:* added the `Ineffective mark-compacts near heap limit` wording, a warning that a snapshot near the memory limit can OOM the pod, and `--heapsnapshot-near-heap-limit`.
- *RB-007:* the doc called the 200 GB cap a protection against a full disk. With about 95 GB free on the 500 GB volume, a stuck slot fills the disk first, and the text now says so. The slot query has `NULLS LAST` (`restart_lsn` is NULL for some slots).
- *RB-003:* failure_types is now `high_cpu` + `api_latency`.
- *Validator:* `RB-NNN` and `INC-NNNN` references are allowed. Any other `RB-`/`INC-` token is a malformed-reference error, which also blocks citing `INC-LIVE-001`.

**Fixes after third review** (`services.toml` catalog and NOTES only; no runbook changed)
- *Catalog meanings that contradicted the runbooks.* I checked every catalog code used in a runbook against how that runbook describes it:
  - `PAYAPI-E1042` still said "within acquire_timeout". It now says the code fires at the app's `DB_ACQUIRE_TIMEOUT` context deadline, not at a pgxpool setting, and that the handler returns `503`.
  - `ORD-5003` said "a downstream dependency was unavailable", but RB-004 says it fires when orders-api's own GC pauses make calls time out while payments-api is healthy. It now says "failed or timed out from orders-api's side (the dependency itself may be healthy)".
  - `PAYAPI-W0310` said only "unavailable", but RB-002 and RB-006 describe a slow score. It now says "unavailable or not returned in time".
  - The other 12 codes the runbooks use match as written.
- *ledger-service release 4.12 dated* 2025-11-12; see Fictional facts.

## Fictional facts (standing: later runbooks, incidents and logs must agree)

Invented details of the fictional company, introduced in batch 1. Reuse them as-is, and don't contradict them. Add new ones here as batches introduce them.

**payments-api / payments-pgbouncer / payments-db** (RB-007, RB-010)
- PgBouncer **1.22**, `pool_mode = transaction`, `max_prepared_statements = 200`, port 6432; pgx v5 default exec mode `cache_statement`, never `simple_protocol`
- pgx pool settings in the `DATABASE_URL` query string, ConfigMap `payments-api-config`: `pool_max_conns=40`, `pool_min_conns=10`, `pool_max_conn_lifetime=30m`, `pool_max_conn_idle_time=5m`
- **`DB_ACQUIRE_TIMEOUT=500ms`** in the same ConfigMap: an app-level context deadline wrapped around every `pgxpool` `Acquire` (pgxpool has no acquire timeout of its own); when it expires, payments-api logs `PAYAPI-E1042` and returns `503`
- 4 pods normally; usual acquired connections 8–15 per pod
- Metrics: `payments_db_pool_acquired_conns`, `payments_db_pool_acquire_wait_seconds`; healthy acquire wait p99 under 50 ms
- pprof on port 6060 (pod-local); DB login user `payments_app`; payment routes `/v1/payments/*`
- payments-db: data dir `/var/lib/postgresql/16/main` on a **500 GB volume** mounted at `/var/lib/postgresql`. Tables and indexes take about 380 GB, normal WAL about 15 GB, and the ballast file 10 GB, so about 95 GB is free on a normal day; 10 GB ballast file at `/var/lib/postgresql/ballast`
- Replication slots: physical `payments_db_02` (the replica), logical `cdc_payments` (feeds the ledger reconciliation pipeline)
- `max_slot_wal_keep_size = 200GB`, chosen to ride out a day-long replica outage, but **above the ~95 GB of free space, so a stuck slot fills the disk before the cap invalidates it**; the 50 GB retained-WAL alert is the real guard. Other alerts: `pg_stat_archiver.failed_count` rising; disk at 85% and 95%
- WAL archiving goes to object storage; dropping a slot needs the DBA lead's approval; an online volume grow takes about 15 min

**ledger-service** (RB-001)
- Spring Boot on Java 17, JVM is PID 1, `-XX:MaxRAMPercentage=75`, GC log at `/var/log/ledger/gc.log`
- Kafka retry workers are threads named `ledger-retry-*`; **release 4.12, shipped 2025-11-12**, added backoff to the retry worker (before 4.12 it spun on broker outages). **Skeleton rule:** no incident whose cause is the retry spin without backoff may start after 2025-11-12. Later Kafka outages can still log `LEDGER-E455`, but the retry worker backs off instead of burning CPU.
- Month-end reconciliation batches are large and CPU-heavy on whichever instance picks them up

**Kafka** (RB-001, RB-008, RB-009)
- Broker ids 1/2/3 = kafka-01/02/03; one data dir `/data/kafka` per broker; tools in `/opt/kafka/bin`; systemd unit `kafka`; broker GC logs in `/var/log/kafka`
- Replication factor 3, `min.insync.replicas=2`; broker default `log.retention.hours=168`
- Topics: `ledger.entries` (**14-day retention**, audit; changes need finance ops' sign-off), `notifications.outbound` (broker default; temporarily cut to 24 h in RB-009's procedure); consumer group `notification-worker`

**search-api / search-cluster** (RB-003, RB-008)
- Index `products` with fields `title` and `sku`; cluster reached at `search-cluster-01.prod.internal:9200`
- search-api feature flag `search.block_leading_wildcard`; incident-time `search.default_search_timeout` of 5s, reverted afterwards
- OpenSearch logs in `/var/log/opensearch` (search-cluster-02 once filled `/var` with an unrotated log)

**orders-api** (RB-004)
- Starts with `--heapsnapshot-signal=SIGUSR2`; `--max-old-space-size` set to about 75% of the memory limit via `NODE_OPTIONS`; working dir `/app`

**fraud-scorer** (RB-002, RB-006)
- gunicorn with `--max-requests 5000 --max-requests-jitter 500`; worker count from `WEB_CONCURRENCY`, falling back to the visible CPU count
- In-process feature cache that grows when redis-features is slow; debug image has `SYS_PTRACE` and `/debug/tracemalloc?top=20` on port 8080
- redis-features is a single node (no failover); latency spikes come from big key scans and BGSAVE

**Platform-wide** (RB-002, RB-005, RB-008)
- Nodes run cgroup v2; payments-api sets `GOMAXPROCS` from its CPU limit in the Deployment; CPU limit raises keep requests unchanged
- Grafana panels "Container CPU" (with a throttling view) and "Container memory (working set)"; throttling above about 25% of periods counts as significant
- Slack channel `#platform-oncall`; journald capped with `SystemMaxUse=500M`; on VMs, `/var` is on the root disk

**Past events mentioned in runbooks: these MUST exist as incidents in the batch 4 skeleton, with matching details**

The runbooks give these as history, so the incident set has to contain them. Each row is a constraint on the skeleton: date, service, hosts, codes and numbers must match. Months are written into the runbooks; years are fixed here.

| When | Source | Incident that must exist |
|---|---|---|
| **March 2025** | RB-002 | fraud-scorer CPU throttling (cause on fraud-scorer, symptom on payments-api): fraud-scorer-01 at **30% throttled periods, 45% average CPU**; payments-api-03 logged `PAYAPI-W0310` at about 40/s; failure types high_cpu / api_latency |
| **October 2025** | RB-001 | ledger-service high CPU from the Kafka retry spin: a broker outage made the `ledger-retry-*` threads spin with no backoff, `LEDGER-E455` logged; fixed by **release 4.12, shipped 2025-11-12**; this is the last retry-spin incident, and none may start after 2025-11-12 |
| **November 2025** | RB-008 | search-cluster-02 `/var` filled by an unrotated `/var/log/opensearch/*.log` (`ENOSPC`); before RB-008's 2025-12-04 review |
| **August 2025** | RB-008 | kafka-02 `/var` filled by broker GC logs in `/var/log/kafka` (`ENOSPC`); `/data/kafka` was not involved |
| **February 2026** | RB-005 | catalog-api-02 OOMKilled about every 50 min: uncapped response cache keyed by full query string, memory climbing linearly |
| **February 2026** | RB-005 | notification-worker-01 OOMKilled only during the Monday-morning email batch; flat memory otherwise, limit below the batch peak (a separate incident from the catalog-api one) |
| 2025-01 to 2025-09 | RB-006 | at least one fraud-scorer memory-growth incident (fraud-scorer-01 or -02) during a redis-features latency spike, with `FRD-W118` and `PAYAPI-W0310`; before RB-006's 2025-09-15 review |
| 2025-01 to 2026-08 | RB-007 | every WAL-disk incident on payments-db has cause *inactive slot* or *archiving failure*, never anything else: at least one of each |
| 2025-04 to 2026-04 | RB-009 | at least one Kafka broker-disk incident in the year before RB-009's 2026-04-11 review, where `ledger.entries` and `notifications.outbound` were the two largest topics |

## Status

- Phase 0 accounts (AWS, Groq, Gemini, Langfuse Cloud, Anthropic credits, GitHub): **not done**. Prince is doing these in parallel. Repo files only until then.

## Unverified claims (check in Phase 0, record results here)

- AWS: Paid plan vs. Free plan behavior (account closing after 6 months), the $200 credits, the credit-unlock activities.
- Groq: current model list, tool-calling support, free-tier limits.
- Gemini Flash: free-tier limits.
- Langfuse Hobby: monthly unit quota.
