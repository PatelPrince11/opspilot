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

## 2026-10-05: Phase 1 step 3, batch 2 (runbooks RB-011 to RB-020)

**Built:** `data/runbooks/RB-011.md` … `RB-020.md` (conn_pool ×3, api_latency ×3, redis_down ×3, dns_failure ×1); 4 catalog codes; formats `reference` and `worked_example` added to the validator; tests updated (20 runbooks, catalog 36 = 20 real + 16 internal).

**Decisions**
- **Topic map for 30 runbooks:** conn_pool 4 (010–013), latency 3 primary (014–016; 002/003 also carry it), Redis 3 (017–019), DNS 3 (020–022), TLS 3, deploy 3, rate limiting 2 (029–030). All 10 failure types covered after batch 3.
- **RB-013 reassigned** from "connection leaks / idle-in-transaction", which RB-010 already covers (rows on `idle in transaction`, `rows.Close()`, `tx.Rollback`), to ledger-service HikariCP pool exhaustion.
- **RB-017/018 split by what the data is:** redis-cache is a cache (losing it floods catalog-db). redis-sessions is a system of record (failover → `READONLY` writes → `AUTH-E401X`). RB-019 is redis-features (single node, refused writes) and never mentions Sentinel.
- **Assumption A amendments:** payments-pgbouncer and internal-dns run on VMs, not Kubernetes (details in Fictional facts). I grepped RB-007 and RB-010 before deciding: neither says Kubernetes or pods for PgBouncer, and no runbook mentioned internal-dns.
- **Catalog adds** (approved): `query_wait_timeout`, `READONLY` and `OOM command not allowed` (real); `FRD-E140` (fraud-scorer). **Design choice, not a measured result:** the catalog holds codes and error tokens. Free-text log phrases (Hikari's timeout text, Envoy flags, Go's `no such host`, PG16's reserved-slot wording) stay out of it, as `context deadline exceeded` did in batch 1. We haven't measured whether this helps or hurts retrieval.
- **Hard negatives scored (word-trigram Jaccard, threshold 0.03):** 010/011 0.0131, 010/012 0.0045, 011/012 0.0096, 010/013 0.0129, 001/013 0.0233, 010/015 0.0108, 017/018 0.0168, 017/019 0.0043, 006/019 0.0157, 014/016 0.0025, 002/014 0.0031, 003/014 0.0048, 004/016 0.0029, 002/016 0.0028. Max over RB-001–020: **0.0233 (RB-001 vs RB-013)**, up from 0.0219 after RB-013 adopted RB-001's `http-nio-*`/Jackson wording on purpose. Threshold unchanged.
- **RB-011 vs RB-010 is separated by PgBouncer-side vocabulary only** (`cl_waiting`, `maxwait`, `SHOW POOLS`, `query_wait_timeout`). RB-010 stays unedited and has none of it. **The June 2025 PgBouncer incident links both RB-011 and RB-010**, because RB-010's row "sessions mostly `active` on the same query" also fits a slow export.
- **013 vs 010 and 015 vs 010:** the primary discriminator is the service's code and database (`LEDGER-W210` + ledger-db; `INV-E220` + inventory-db vs `PAYAPI-E1042` + payments-db), not the language.
- **Batch 3:** the rate_limiting runbooks (RB-029, RB-030) must be scored as hard negatives against RB-016 (shared `CPX-W429`).
- **October 2025 check:** the existing October 2025 ledger retry-spin row names only ledger-service, `ledger-retry-*` and `LEDGER-E455`; no skeleton constraint uses auth-service-01 for it. The new October 2025 RB-018 event (auth-service-01, redis-sessions) shares only the month.
- **Checked against primary sources this round:**
  - PG16 `postinit.c`/`proc.c` (which 53300 message which role sees)
  - PG12 release notes (WAL senders don't count toward `max_connections`)
  - Redis Sentinel docs (failover needs a majority; two Sentinels discouraged → 3 Sentinels)
  - Redis admin docs and FAQ (overcommit)
  - Linux `mm/util.c`
  - CoreDNS `forward`, `cache` and `file` docs
  - Kubernetes DNS docs (default Corefile `cache 30`; pod `ndots:5`)
  - PgBouncer config and usage docs
  - go-redis `error.go`/`sentinel.go`

**Errors and fixes**
- *Outline review, pre-draft:*
  - RB-011's first June 2025 figure (`cl_waiting` ≈ 60) was impossible: about 80 clients per bouncer against 40 + 10 server connections caps it near 30, and the reserve opens only after `reserve_pool_timeout` (3 s).
  - The event also claimed `query_wait_timeout` errors with `maxwait` ≈ 4 s, under the 10 s limit; dropped.
  - Two Sentinels can't fail over after losing a VM (no majority), so there are now three.
  - RB-017's failover time now includes the 30 s detection (≈ 40 s as clients saw it).
  - RB-019's first mechanism (fork failing under `overcommit_memory = 0`) doesn't hold on current kernels, where mode 0 refuses only allocations above RAM + swap. It now uses mode 2. The Redis FAQ still describes the old mode-0 behavior; RB-019 doesn't restate it.
  - RB-018's first story invented go-redis behavior; go-redis actually drops connections on `READONLY` and asks Sentinel again, so the claim is reduced to a few-second window.
  - RB-020 says "intermittent" rather than "about half": the CoreDNS cache docs don't say how the negative TTL is derived from the SOA.
- *Draft:*
  - RB-006 vs RB-019 measured **0.0407**, over 0.03. Root cause: the two runbooks share the redis-features hostname on every `redis-cli` line, and I had copied RB-006's `kubectl -n risk logs deploy/fraud-scorer --since=15m | grep -c` idiom and its "fraud-scorer-01… and fraud-scorer-02…" sentence. Fix: a shell function wraps the repeated `redis-cli -h …` prefix (a function rather than `$R`, because zsh doesn't word-split unquoted variables), and the log line and history sentence were rewritten. Now 0.0157. The threshold was not raised.
- *Test:* `test_manifest_hashes_committed_outputs_only` failed (`assert 20 == 10`). Root cause: it hard-codes the runbook count, and my test-change list covered only the id-range test. Fixed to 20.

**Fixes after review** (RB-011, 012, 014, 015, 017, 018, 020; RB-010 unchanged, `git diff 1a5d32c -- data/runbooks/RB-010.md` empty)
- **RB-017:** removed the hand-promotion path (`REPLICAOF NO ONE`, `SENTINEL RESET`). Now: if there's no majority, restore a Sentinel; otherwise page team-platform's lead; never promote by hand. The catalog-db escalation pages team-catalog and team-platform.
- **RB-011:**
  - An uneven bouncer split now goes to team-payments, with no restart. RB-011 no longer says how payments-api spreads connections; batch 4 decides that.
  - Dropped the unsupported last sentence of "If waits pass 10 seconds".
- **RB-020:** checked against glibc `resolv/res_send.c` (master). Over UDP (`send_dg`), SERVFAIL, NOTIMP and REFUSED move on to the next nameserver. NXDOMAIN is returned as the final answer. TCP (`send_vc`) doesn't inspect the rcode.
  - The VM paragraph and the SERVFAIL fix now match this.
  - Removed the unverified claim that Go doesn't try another server.
  - The log pipeline now extracts `lookup <name> on <resolver>: <error>` before `uniq -c`.
- **RB-015:** query D adds `state <> 'idle'`.
- **RB-018:** the restart answer now says a restart shorter than `down-after-milliseconds` (30 s) triggers no failover but drops every connection. On data, it says only what the Redis admin docs support: a primary that restarts empty wipes its replicas, so data depends on persistence.
- **RB-012:** I couldn't confirm from PgBouncer's own docs or source that it logs a server-side 53300 refusal; I found only third-party guides and 2017 mailing-list logs. The sentence is now "also check the PgBouncer log on payments-pgbouncer-01/02".
- **RB-014:** the `/search` row says "slow" only; rejections are search-api's own errors.
- **Envoy flags** `UT`/`UF`/`UO`/`URX`/`UH` checked against Envoy's substitution-formatter docs (current release). **The strict-mode `CommitLimit` formula** checked against the kernel docs `filesystems/proc` (meminfo): `CommitLimit = (RAM pages − huge TLB pages) × overcommit_ratio / 100 + swap pages`. RB-019's 32 GB × 80% + 0 swap ≈ 25.6 GB matches, with no huge pages.
- *Rerun:* the first rerun failed (`make corpus`: docker API socket missing; `runbooks.jsonl is stale` in the tests). Root cause: Docker Desktop had stopped, so the corpus wasn't rebuilt. I restarted Docker Desktop and the stack (`make up`) and reran everything; all green. Scores above are after the review fixes.
- **RB-001 vs RB-013 thread-name conflict, resolved in RB-013 only.** RB-001:68 attributes month-end CPU to an `http-nio-*` thread in Jackson serializer frames. RB-013 said `recon-worker-*` threads were doing the serialization. RB-013 now says `top -H` showed the `http-nio-*` serializer thread as the top CPU thread and that `recon-worker-*` threads held the connections. The split is recorded in Fictional facts. RB-001 is unchanged.

**Follow-up (separate commit):** RB-016 now refers to one incident (May 2026), not two. RB-014 drops the `URX` row and the edge-retry checks, because no edge retry policy is defined. RB-020's payments-api sentence now matches services.toml (via the payments-pgbouncer names). RB-017's CKQUORUM line explains quorum vs majority with three Sentinels. RB-018 restarts the Deployment. RB-013's timeline is now 23:35 → `503` within a minute → 03:10 finish; the ConfigMap holds parallelism 4, and in-flight chunks were redone.

**Verified:** `make corpus` gives 20 runbooks, and a second run produced a byte-identical MANIFEST. `make corpus-check` ok. `make test`: 61 passed with the stack up. `make lint` clean.

## 2026-10-07: Batch 1 and 2 upstream-doc checks

The batch 1 uncertainty list was **reconstructed from the 2026-10-04 batch 1 transcript, not from NOTES**. NOTES line "Not yet verified against upstream docs" named the list but never enumerated it, and no corrections had been recorded against it. Each item below was read against a primary source (docs or source at the named version) on 2026-10-07.

**Items confirmed**
- *PgBouncer 1.22* (`doc/usage.md`, `doc/config.md`, `src/janitor.c`, `src/proto.c`, NEWS):
  - `SHOW POOLS` columns `cl_waiting`, `maxwait`, `sv_active`, `sv_idle`
  - `SHOW SERVERS` lists server connections
  - server login uses the client user name when `[databases] user=` is unset
  - `max_prepared_statements` exists since 1.21
  - `query_wait_timeout` reaches the client as FATAL `query_wait_timeout`, SQLSTATE `08P01`, and is logged as `pooler error: query_wait_timeout`
- *Postgres 16* (docs and `REL_16_STABLE` source):
  - `wal_status` values
  - `max_slot_wal_keep_size`: default −1, `PGC_SIGHUP` (reload is enough)
  - `pg_stat_archiver` and `pg_stat_wal_receiver` columns
  - the `could not write to file "%s": %m` text
  - 57P02 "terminating connection because of crash of another server process"
  - 57P01 "terminating connection due to administrator command"
  - `restart_after_crash` defaults to on
  - `CHECKPOINT` lets unneeded WAL be recycled or removed
  - `pg_stat_activity` `state`, `state_change`, `xact_start`, `idle in transaction`
  - the 53300 messages and which role sees which
  - WAL senders don't count toward `max_connections`
  - Debian `/var/lib/postgresql/<version>/<cluster>` and `postgresql@<version>-<cluster>`
- *Kubernetes, kernel and Go*:
  - cgroup v2 `cpu.stat` fields `nr_periods`, `nr_throttled`, `throttled_usec`
  - cAdvisor `container_cpu_cfs_periods_total` and `container_cpu_cfs_throttled_periods_total`
  - GOMAXPROCS is container-aware only from Go 1.25
  - `OOMKilled` with exit code 137, and the `lastState.terminated.reason` jsonpath
  - containerd reports reason `Error` for a non-zero, non-OOM exit
  - the `Evicted` message format "The node was low on resource: %v."
  - the eviction ranking (usage over requests, then priority, then usage relative to requests; QoS class not used)
  - `kubectl cp` needs `tar`
  - `kubectl logs deploy/x` reads the first pod
  - **`kubectl logs -l` defaults `--tail` to 10**
  - Go `lookup <name> on <server>: <err>`, `no such host`, `server misbehaving`, and no retry on NXDOMAIN (`net.go`, `dnsclient_unix.go`)
  - glibc `res_send.c`: over UDP, SERVFAIL/REFUSED/NOTIMP try the next server; NXDOMAIN is final
- *JVM* (JDK 17 man pages, `jdk17u` HotSpot source):
  - `jcmd Thread.print` and `GC.heap_info`
  - `jstat -gcutil` columns `O`, `FGC`, `FGCT`
  - thread-dump `cpu=`, `elapsed=` and hex `nid=0x%x`
  - GC thread names `GC Thread#N`, `G1 Conc#N`
  - `-XX:MaxRAMPercentage` and the `-Xlog:…:file=` syntax
- *HikariCP 5.0.1*:
  - timeout message `… request timed out after <n>ms.` (no pool-state suffix before 6.x)
  - `connectionTimeout` default 30000
  - `HikariPool-` name prefix
  - the leak-detection message
- *Node and V8* (main):
  - `FATAL ERROR: Reached heap limit` and `Ineffective mark-compacts near heap limit` with "Allocation failed - JavaScript heap out of memory"
  - the heap-snapshot flags are allowed in `NODE_OPTIONS`
  - snapshots go to the working directory, need about 2× the heap, and block the event loop
- *Python and Redis*:
  - py-spy needs `SYS_PTRACE`, and `top --subprocesses` exists
  - gunicorn `WEB_CONCURRENCY`, `--max-requests`, `--max-requests-jitter`
  - `redis-cli --latency`
  - the fork for BGSAVE can add latency
  - Redis admin and FAQ text on overcommit
- *OpenSearch* (docs and `ExceptionsHelper.java`):
  - `_cat/thread_pool` columns
  - `running_time_in_nanos`
  - the task id format and cancel path
  - `hot_threads`
  - `search.default_search_timeout` is dynamic with default −1
  - a rejected execution returns HTTP 429
- *Kafka 3.8* (generated config docs and source):
  - `kafka-log-dirs.sh --describe --broker-list`
  - `kafka-configs.sh --add-config` / `--delete-config`
  - `log.retention.hours` 168, retention check every 5 min, `file.delete.delay.ms` 60000
  - `retention.bytes` is per partition
  - the active segment is never deleted (`segment.ms`)
  - a broker shuts down when all log dirs fail
  - `acks=all` with `min.insync.replicas` → NotEnoughReplicas
- *Linux man pages*: `du -x`/`--max-depth`, `journalctl --vacuum-size`, `SystemMaxUse=`, `df -i`, `copytruncate`, `lsof +L1`.
- *Envoy and kernel*: Envoy flags `UT`/`UF`/`UO`/`URX`/`UH`; the strict-mode `CommitLimit` formula (`filesystems/proc`).

**Items unverified**
- (5) RB-007:35: the PANIC level on that particular write. The text is confirmed, and `elog.c` promotes ERROR to PANIC inside a critical section, but I didn't trace the call path.
- (6) RB-007:17: that `pg_archivecleanup` is unsafe on `pg_wal`. The docs are silent, and the line is a prohibition, so it's left as is.
- (9) RB-008:20: that truncating through `/proc/<pid>/fd/<fd>` frees the space. `proc_pid_fd(5)` says only that the entry links to the open file.
- (10) RB-001:45, :57, :59, :80: `jcmd` and `top` attaching to PID 1 inside the container. No source checked.
- **CPU-limit arithmetic (RB-002:14):** the kernel docs confirm the 100 ms default period, and the 2-CPU / 16-thread figures are plain arithmetic. But the Kubernetes docs only say CPU limits "are enforced by CPU throttling". They don't state that a limit becomes a CFS quota over a 100 ms period, so that mapping stays unverified against Kubernetes' own docs.

**Wrong, fixed:**
- RB-005:38 claimed pods with no requests are evicted before everything else; the eviction docs rank by usage over requests, then priority, then usage relative to requests (batch 1 commit).
- RB-019:23 counted `FRD-E140` through `kubectl logs -l`, which tails 10 lines per pod by default; `--tail=-1` added.

**Reworded, because unverified:**
- RB-007:38 and :127: the "can't finish crash recovery on a full disk" claim is gone.
- RB-011:88: the pgx claim is replaced by the confirmed FATAL/`08P01` text.

**Restored with source:** RB-020:32, Go doesn't try another server after NXDOMAIN.

**Applied:** the RB-003:36 `detailed=true` wording (edit 7), after Prince approved editing RB-003 for that change only (separate commit).

**New facts:** HikariCP 5.x for ledger-service; private cgroup namespace for containers. Checked against RB-002:16, which already says the file is at the cgroup root inside the container, so there's no contradiction.

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
- Spring Boot on Java 17; **ledger-service uses HikariCP 5.x** (its timeout message ends `ms.` with no pool-state suffix); JVM is PID 1, `-XX:MaxRAMPercentage=75`, GC log at `/var/log/ledger/gc.log`
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
- Nodes run cgroup v2; **containers run with a private cgroup namespace** (so `/sys/fs/cgroup/cpu.stat` inside a container is the container's own); payments-api sets `GOMAXPROCS` from its CPU limit in the Deployment; CPU limit raises keep requests unchanged
- Grafana panels "Container CPU" (with a throttling view) and "Container memory (working set)"; throttling above about 25% of periods counts as significant
- Slack channel `#platform-oncall`; journald capped with `SystemMaxUse=500M`; on VMs, `/var` is on the root disk

**Batch 2 facts approved in review:** namespaces `commerce` and `identity`; systemd units `redis-server` and `redis-sentinel`; `reservations_expires_at_idx`; zone file `/etc/coredns/zones/prod.internal.db`; Envoy admin on port 9901; route `/checkout`.

**Assumption A amendments** (batch 2)
- **payments-pgbouncer runs on two VMs**, not Kubernetes: systemd unit `pgbouncer`, `/etc/pgbouncer/pgbouncer.ini`, admin console `psql -h <host> -p 6432 -U pgbouncer pgbouncer`. Each bouncer normally carries about half of payments-api's connections. How payments-api picks a bouncer, and how it falls back, is **not decided**; batch 4's demo spec decides it. (RB-011)
- **internal-dns runs on two VMs**: CoreDNS, systemd unit `coredns`, `/etc/coredns/Corefile`.
  - Authoritative for `prod.internal` via the `file` plugin, zone file `/etc/coredns/zones/prod.internal.db`, reloaded when the SOA serial changes. Record TTL 60 s, SOA negative TTL 60 s. Each VM has its own copy; the rollout tool updates them one after the other.
  - Kubernetes cluster CoreDNS has a `prod.internal` server block with `cache 30` and `forward . 10.0.0.53 10.0.0.54` (default `random` policy). Pods use the Kubernetes default `ndots:5`.
  - VMs list internal-dns-01, then -02, in `/etc/resolv.conf`, with no `options rotate`. (RB-020)

**payments-pgbouncer** (RB-011, RB-012)
- Each instance: `default_pool_size = 40`, `reserve_pool_size = 10`, `reserve_pool_timeout = 3`, `max_client_conn = 500`, `query_wait_timeout = 10`
- 4 × 40 = 160 client connections, about 80 per bouncer; at most 2 × 50 = 100 server connections on payments-db

**Postgres connection budgets** (RB-012)
- `max_connections`: payments-db 200, ledger-db 200, orders-db 300, auth-db 200, inventory-db 100, catalog-db 100; `superuser_reserved_connections = 3` and `reserved_connections = 0` everywhere
- App pools: orders-api `pg` Pool `max` 25 × 3 pods; auth-service pgxpool `pool_max_conns` 30 × 2; catalog-api SQLAlchemy `pool_size` 10 + `max_overflow` 5 × 2; inventory-service pool 20 × **2 pods** (80 during the April 2026 release); ledger-service Hikari `maximumPoolSize` 20 × 2
- DB roles `inventory_app`, `ledger_app` (alongside `payments_app`)

**ledger-service** (RB-013)
- HikariCP pool `HikariPool-1`, `connectionTimeout` default 30 s, `leakDetectionThreshold = 60000`; metrics `hikaricp_connections_active` / `_pending`
- Month-end reconciliation is triggered over HTTP. Result serialization runs on the request (`http-nio-*`) thread, which matches RB-001. Chunk work runs on `recon-worker-*` threads, each holding one Hikari connection per chunk transaction. One transaction per chunk of accounts; resumes from its last committed chunk after a restart. `LEDGER_RECON_PARALLELISM` was 24 until the January 2026 incident and was changed to **4 on the night of 2026-01-31/02-01** (RB-013). **Skeleton rule:** no ledger-service month-end HikariCP exhaustion caused by reconciliation parallelism may start after 2026-02-01. A separate reconciliation pool is proposed, **not built**.

**inventory-service / inventory-db** (RB-012, RB-015, RB-020)
- Namespace `commerce`. Table `reservations` (`sku`, `status`, `expires_at`); index `reservations_expires_at_idx` built after the July 2025 incident

**edge-gateway** (RB-014, RB-016)
- Namespace `platform`; route timeout 15 s; Envoy admin on port 9901 (pod-local); routes `/v1/payments/*`, `/checkout`, `/search`

**Checkout retries** (RB-016)
- orders-api → payments-api `PAYMENTS_MAX_ATTEMPTS = 3`; payments-api → card-processor-proxy `CPX_MAX_ATTEMPTS = 3` (ConfigMaps; RB-016 sets it to 1 during an incident and restores 3 afterwards); no deadline propagation between orders-api and payments-api; retry budgets, jittered backoff and deadline passing are **not built**

**Redis** (RB-017, RB-018, RB-019)
- **Sentinel:** 3 processes (systemd `redis-sentinel`, port 26379) on redis-cache-02, redis-sessions-02 and internal-dns-01. They monitor **only** masters `cache` and `sessions`, never redis-features. Quorum 2; `down-after-milliseconds 30000`.
- **catalog-api** cache hit ratio about 95%.
- **payments-api and inventory-service** cache only reference data in redis-cache, with an in-process copy refreshed every 60 s, served on Redis errors.
- **auth-service** writes a session at login and looks it up on each request. If a session write fails, it logs the error and completes the login anyway; the session isn't stored, and the next lookup fails with `AUTH-E401X`. Namespace `identity`.
- **redis-features:** 32 GB RAM VM, no swap, `vm.overcommit_memory = 1` (team-platform's OS hardening baseline briefly set 2 with ratio 80 in September 2025); systemd unit `redis-server`; `maxmemory 24gb`, `maxmemory-policy noeviction`, `stop-writes-on-bgsave-error yes`.
- **fraud-scorer** writes per-card velocity counters (INCR + TTL) on every score; `FRD-E140` means they go stale.

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
| **June 2025** | RB-011 | **Must exist as an incident.** Both payments-pgbouncer instances saturated by a refunds export inside payments-api (about 2 s transactions). `cl_waiting` about 30 per instance, `maxwait` about 4 s; pgx pools filled and `PAYAPI-E1042`/`503` followed. **No `query_wait_timeout`.** Types conn_pool / api_latency. **runbook_ids include RB-011 and RB-010.** No WAL or disk involvement; no demo details. |
| **January 2026** (31 Jan, `LEDGER-W210` from about 23:35; batch finished 03:10 on 1 Feb) | RB-013 | **Must exist as an incident.** ledger-service-02 HikariCP pool 20/20, `hikaricp_connections_pending` up to about 40, held by `recon-worker-*` threads (`LEDGER_RECON_PARALLELISM=24`). `LEDGER-W210`, then `request timed out after 30000ms`; `503` to payments-api. CPU elevated, not saturated. Fixed by parallelism 4 and a pod restart. **Not a retry-spin incident.** |
| **April 2026** | RB-012 | **Must exist as an incident.** inventory-db-01 `53300` ("remaining connection slots are reserved for roles with the SUPERUSER attribute") after an inventory-service release raised its pool from 20 to 80 per pod (2 × 80 = 160 > 97 usable). Cause bad_deploy, symptom conn_pool; fixed by rollback. |
| **July 2025** | RB-015 | **Must exist as an incident.** inventory-db-01 sequential scans on `reservations` after a release added an `expires_at` filter with no index; `INV-E220`, `ORD-5003`; fixed the same afternoon by building `reservations_expires_at_idx` concurrently. |
| **May 2026** | RB-016 | **Must exist as an incident.** Processor slowdown → `CPX-W429` → retries in payments-api and orders-api; `PAYAPI-E1107`, `ORD-5003`, edge `504` for about 40 minutes; ended within minutes of `CPX_MAX_ATTEMPTS=1`. Cause rate_limiting, symptom api_latency. **Not ledger-service.** |
| **December 2025** | RB-017 | **Must exist as an incident.** redis-cache-01 VM lost; outage as clients saw it about 40 s (30 s detection + failover to redis-cache-02), `ETIMEDOUT` until rediscovery. catalog-api `CAT-E301`, catalog-db-01 CPU about 90%. payments-api and inventory-service logged Redis timeouts **with no extra DB reads** (60 s in-process copies). |
| **October 2025** | RB-018 | **Must exist as an incident.** Planned `SENTINEL FAILOVER sessions` before patching redis-sessions-01. For a few seconds, before auth-service-01 learned the new master, session writes that still failed after the client's retries got `READONLY` and weren't stored; those users hit `AUTH-E401X` on their next request. Few users affected. No other session loss. |
| **September 2025** | RB-019 | **Must exist as an incident.** team-platform's OS hardening baseline set `vm.overcommit_memory = 2` (ratio 80) on redis-features-01. Redis at about 14 GB; the BGSAVE fork failed (`fork: Cannot allocate memory`) → `MISCONF` → `FRD-E140` on fraud-scorer-01/02. Fixed by restoring `1`. Before RB-019's 2025-11-03 review. |
| **March 2026** | RB-020 | **Must exist as an incident.** A zone rollout reached only internal-dns-02 and dropped `inventory-db-01.prod.internal`. inventory-service-01/02 logged `no such host` **intermittently**; VMs unaffected because their resolv.conf lists internal-dns-01 first. Before RB-020's 2026-04-08 review. |

## Status

- Phase 0 accounts (AWS, Groq, Gemini, Langfuse Cloud, Anthropic credits, GitHub): **not done**. Prince is doing these in parallel. Repo files only until then.

## Unverified claims (check in Phase 0, record results here)

- AWS: Paid plan vs. Free plan behavior (account closing after 6 months), the $200 credits, the credit-unlock activities.
- Groq: current model list, tool-calling support, free-tier limits.
- Gemini Flash: free-tier limits.
- Langfuse Hobby: monthly unit quota.
