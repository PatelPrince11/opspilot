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

## 2026-10-07: Phase 1 step 3, batch 3 (runbooks RB-021 to RB-030)

**Built:**
- `data/runbooks/RB-021.md` … `RB-030.md`: dns_failure ×2, tls_expired ×3, bad_deploy ×3, rate_limiting ×2. All 10 failure types are now covered, and the 30 runbooks are complete.
- 2 catalog codes
- tests updated: 30 runbooks; catalog **38 = 21 real + 17 internal**

**Decisions**
- **Catalog adds** (approved):
  - `EAI_AGAIN` (real, errno-style like `ETIMEDOUT`)
  - `CPX-W430` (internal): the proxy's own egress limiter
  - Option (b) was chosen over widening `CPX-W429`, so no existing meaning changes and RB-016:27 stays exactly true.
- **Formats:** every one of the 10 formats now appears exactly 3 times.
- **Owners:** team-growth has its first runbook (RB-030); team-platform took 3 of the 10.
- **No `RB-NNN` cross-references in batch 3**, so no runbook is contrasted with its hard negatives.
- **Demo-guard scratch rule:** flag a runbook with (config-push trigger and `payments-pgbouncer-01`) or (`ECONNREFUSED` and `6432`). On RB-001..030 it flags nothing. `ECONNREFUSED` occurs 0 times in all 30 runbooks. RB-027 is the config-push runbook: it uses Kafka, never names PgBouncer, and quotes Java's "could not be established" text, not `ECONNREFUSED`. The test's `leaks_demo` is unchanged.
- **Binding constraint for the batch 4 demo spec:** the rollout tool's health gate (since 2026-08-20) covers only the units in its probe table, and **payments-pgbouncer is not enrolled**. A unit outside the table still gets the older pacing: canary, a fixed 5-minute wait, then the next host, with only the exit status of `systemctl restart` checked. So the demo's config push to payments-pgbouncer-01 is consistent only while PgBouncer stays outside the gate. If batch 4 enrolls it, or wants the gate to have caught the failure, the spec must say so explicitly.
- **ndots amendment:** RB-020's "pods use ndots:5" stays true for every pod except orders-api from 2026-06-18 (RB-021). RB-020 is unedited and was accurate at its 2026-04-08 review.
- **Hard negatives scored** (word-trigram Jaccard; ceiling 0.025, test threshold 0.03 unchanged):

  | Pair | Score |
  |---|---|
  | 020/021 | 0.0061 |
  | 004/021 | 0.0043 |
  | 014/021 | 0.0032 |
  | 020/022 | 0.0098 |
  | 021/022 | 0.0028 |
  | 002/022 | 0.0018 |
  | 016/023 | 0.0046 |
  | 023/024 | 0.0090 |
  | 023/029 | 0.0089 |
  | 012/024 | 0.0016 |
  | 010/024 | 0.0044 |
  | 014/025 | 0.0037 |
  | 023/025 | 0.0056 |
  | 018/026 | 0.0141 |
  | 009/027 | 0.0143 |
  | 001/027 | 0.0022 |
  | 020/027 | 0.0056 |
  | 015/028 | 0.0085 |
  | 017/028 | 0.0075 |
  | 012/028 | 0.0021 |
  | 016/029 | 0.0082 |
  | 016/030 | 0.0019 |
  | 003/030 | 0.0026 |
  | 014/030 | 0.0126 |
  | 029/030 | 0.0027 |

  Max over RB-001–030 is still **0.0233 (RB-001 vs RB-013)**. Word counts for 021–030 are 446–852 (after the follow-up fixes; scores above are re-measured).
- **Quote check:** all 49 committed lines cited in the batch 3 hard-negative table were re-read at HEAD `b320d6c` and match.
- **Checked against primary sources this round:**
  - pgx `pgxpool/pool.go`: `defaultMaxConnLifetime = time.Hour`, `defaultMaxConnIdleTime = time.Minute * 30`; `pool_max_conn_*` parsed from the URL
  - SQLAlchemy `pool/impl.py`: QueuePool `timeout: float = 30.0`, message "QueuePool limit of size %d overflow %d reached, connection timed out, timeout %0.2f"
  - node-postgres `pg-pool/index.js`: `idleTimeoutMillis = 10000`
  - Kafka 3.8 `ConfigException` ("Invalid value … for configuration …: …") and `ConfigDef.parseType` ("Not a number of type " + type)
  - Envoy listener stats: `listener.<address>.ssl.connection_error`, "Total TLS connection errors not including failed certificate verifications"
  - Kubernetes DNS docs: `dnsConfig` options `ndots` "2", merged with the policy's options
  - resolv.conf(5): timeout default 5, attempts default 2
  - Node `dns` docs: `dns.lookup` runs `getaddrinfo` on libuv's threadpool
  - PG16 docs:
    - SSL files read at start and on every configuration reload
    - `server.key` must be 0600 (or root-owned 0640)
    - `pg_file_settings` columns and behavior
    - `pg_blocking_pids` "soft block": a waiter ahead in the queue blocks later requests
    - ACCESS SHARE conflicts only with ACCESS EXCLUSIVE

**Errors and fixes**
- *Outline review, pre-draft:*
  - **RB-027 timeline.** 10:12 and 10:13 implied kafka-03 at 10:14, yet the run was stopped at 10:19 "before kafka-03". The tool's pacing is now stated (canary, then a fixed 5 min). kafka-02 restarts at 10:17, the run stops at 10:20 with kafka-03 due at 10:22, and the write outage is 10 minutes, not 13.
  - **RB-027's other Kafka dependents** are now accounted for: the orders-api outbox relay (no `ORD-5003`) and notification-worker (lag flat, no `NTF-E710`).
  - **RB-028:** "every read stalls" ignored RB-017's 95% cache hit. Only cache misses (about 60/s) and writes (about 5/s) queue. 30 connections fill in about 0.46 s, which fixes 14:02:15 (edge 504) and 14:02:30 (QueuePool).
  - **RB-029:**
    - Retries take limiter tokens. With `CPX_MAX_ATTEMPTS=3`: r³ = 0.25, r ≈ 0.63, about 487 attempts/s, 60/s `PAYAPI-E1107`.
    - 240/s is a steady-state measured arrival rate, with the orders-api retry feedback ignored.
    - A rejected attempt waits the full 2 s.
    - The Pending pod is explained by the egress-pool allowlist plus anti-affinity.
    - The uncordon waited for the patch and reboot.
  - **RB-024:** no percentage of failing logins; it says only that new connections fail and older ones age out over the hour.
  - **RB-021:** no mechanism is claimed for the lost UDP reply ("now and then a reply is lost").
  - **Demo-guard scratch rule:** the first version ("any two of four tokens") would have false-flagged RB-011.
- *Draft:*
  - RB-014 vs RB-025 first measured **0.0244**, just under the 0.025 ceiling. Root cause: RB-025 copied RB-014's admin-port idiom (`kubectl -n platform port-forward pod/<edge pod> 9901:9901` then `curl -s localhost:9901/stats…`) and its "access log line" / "on every route" wording. RB-025 was reworded and now scores 0.0037 (after the follow-up fixes). RB-014 is untouched.
  - RB-027 step 1 first said `pg_file_settings` "lists lines the server would reject". The PG16 docs say a setting that can't change after start also shows a message in `error`. Reworded so each row gets read.
  - *Rerun:* `make test` and `make corpus-check` failed with `runbooks.jsonl is stale`. Root cause: I edited RB-027 after the last `make corpus`. Rebuilt; all green.

**Verified:** `make corpus` produced 30 runbooks; `make corpus-check` ok; `make test` 61 passed; `make lint` clean.

**Follow-up fixes** (RB-021, 023, 025, 028, 029, 030; facts updated)
- **RB-021:** the 10 s group is two lost replies on *different* queries, each recovered by its retry: a slow `200`, not an error. `EAI_AGAIN` needs both tries of *one* query lost (`attempts:2`). The 21:40 paragraph said two losses in a row end in `EAI_AGAIN`, which mixed the two cases.
- **RB-023:**
  - Step 6 now says a restart rolls one proxy at a time (maxSurge 0 / maxUnavailable 1, new fact), halving the budget while it rolls.
  - The sample counts are labeled off-peak (2,071 in 10 min ≈ 3.5/s per pod).
  - Because step 6 now names `CPX-W430` (which exists only from 2026-07-09), `last_reviewed` moved from 2026-01-19 to **2026-09-22**. Otherwise the review date would predate the code it cites.
- **RB-029:** lever 2 checked against the new rollout settings and reworded. With one pod Pending, maxUnavailable 1 is already used up, so the running pod must be deleted by hand, leaving a gap with no proxy.
- **RB-028:** the `pg_stat_activity` query is now ordered by `xact_start`, and the voice is third person throughout.
- **Envoy, checked against the docs (latest) on 2026-10-07:**
  - *Local rate limit filter page:*
    - "If the local rate limit token bucket is checked, and there are no tokens available, a 429 response is returned (the response is configurable)."
    - "By default the rate limits are applied per Envoy process."
    - stats under `<stat_prefix>.http_local_rate_limit.`: `rate_limited` is "Total responses without an available token (but not necessarily enforced)"; `enforced` is "Total number of requests for which rate limiting was applied (e.g.: 429 returned)"
  - *Substitution formatter, `RL`:* "The request was rate-limited locally by the HTTP rate limit filter in addition to 429 response code."
  - *Listener stats:* `listener.<address>.ssl.connection_error` is "Total TLS connection errors not including failed certificate verifications", with no further detail; `handshake` is "Total successful TLS connection handshakes".
- **Envoy changes that follow from the docs:**
  - **RB-030** drops the `RL` flag. The docs tie `RL` to "the HTTP rate limit filter" without naming the local filter, so it isn't claimed for our limiter.
  - **RB-030** reads the `enforced` counter instead of `rate_limited`, because `rate_limited` counts token misses that weren't necessarily rejected.
  - **RB-025** no longer claims `connection_error` climbs when clients reject our certificate. It now watches `ssl.handshake` dropping off, and says the docs don't settle whether `connection_error` counts this case.
- **Facts added:** proxy maxSurge 0 / maxUnavailable 1; auth-db-01 primary, auth-db-02 replica.

## 2026-10-08: Phase 1 step 3, batch 4 (incident skeleton + demo spec)

**Built:**
- `data/incidents/skeleton.toml`: 200 rows (INC-0001..0200), with no prose and no logs
- `eval/corpus/incidents_sidecar.toml`: eval-only fields, never ingested
- `eval/corpus/demo_answer.toml`: INC-LIVE-001, with the mechanism still a placeholder
- `scripts/corpus/incidents.py`: the schema and catalog validator, called by `build_committed()`, so `make corpus` and `corpus-check` fail on a broken row
- `tests/test_incidents.py` (43 tests); `leaks_demo` now reads `leak_combination` from `demo_answer.toml`

No new committed output yet: MANIFEST is unchanged, and `incidents.jsonl` arrives with the prose.

**Decisions**
- **Row fields** (in `data/`):
  - `service` (the cause service of record), `symptom_services`, `severity`, UTC `started_at`/`resolved_at`
  - `failure_types` (cause type first), `codes`, `hosts`, `runbook_ids` (0–3), `synthetic`
- **Sidecar fields** (in `eval/corpus/`): `constraint`, `mechanism`, `cause_component`, `absent_codes`, `anchor_tokens`, `multi_doc`, `hard_negative_pair` + `discriminators`, `facts`.
- **Ids are chronological** by `started_at` within fixed month blocks, so the constraint ids were fixed before generation. Changing a date later means renumbering.
- **Constraint rows:** the NOTES past-events table has 27 rows, which became **29 incidents**: RB-007's row needs ≥1 inactive-slot and ≥1 archive incident, plus a third WAL incident so RB-007 has 3 links. All 29 are pinned field by field in `test_constraint_rows_match_exactly`.
- **Rulings (Prince):**
  - INC-0016 is filed under internal-dns with `cause_component = "cluster CoreDNS (kube-system)"`; its codes are `EAI_AGAIN` only.
  - R14–R19 and R21 approved.
  - tls_expired 14 and rate_limiting 16 (the facts close most paths for these).
  - Demo option B; facts F1–F5 (below).
- **Fillers (171)** came from a seeded one-off generator in the scratchpad (per-type templates: service, mechanism, codes, hosts, runbooks, date window). It isn't committed; the TOML is the source of truth.
- **Filler placement rule:** fillers that link a runbook whose history names a single event (RB-019, RB-022, RB-024) are dated after that runbook's review, and so are search-suffix latency (RB-021) and campaign search rejections (RB-030). Otherwise those runbooks would read as unaware of a later or earlier sibling.

**Errors and fixes**
- *INC-0124 host ownership.* The validator rejected `ledger-db-01` because ledger-db wasn't among the row's services. Root cause: the outline listed the host but not ledger-db as a symptom. Fix: `symptom_services = ["payments-api", "ledger-db"]`, a deviation from the approved outline table.
- *inventory-db never a cause* (`3 <= 0` in the bounds test). Root cause: my templates never used it, although the outline targeted 6. Fix: 3 rows moved to inventory-db (a `/var` row, a table-bloat latency row, a 53300 row). It now sits at 3, the lower bound.
- *Severity skew.* The first draw gave sev1 5 and sev3 107 against targets of 16 and 90 (±5). Root cause: the template defaults leaned to sev3. Fixed per template: final 16/56/87/41.
- *RB-028 and RB-030 under 3 links.* RB-028 was linked to a QueuePool row and a post-2025-05-16 `lock_timeout` retry row; RB-030 to a campaign-driven `SRCH-E502` row. Neither creates a hard-negative pair.
- *Narrative conflicts found in the spot check* (e.g. a 2025-01 `MISCONF` row linking RB-019, whose history lists only 2025-09). Fixed by the placement rule above.

**Verified:**
- `make corpus` and `make corpus-check` ok
- `make test`: 104 passed (43 new)
- `make lint` clean
- `git diff --stat data/runbooks` is empty

**Spot-check fix:** generated fillers could name payments-db-02, the replica (e.g. INC-0009), though no fact says payments-api reads from it. Every payments-db filler host is now payments-db-01. payments-db-02 appears only in INC-0174 (replica slot).

**Follow-up after review** (skeleton, sidecar and tests only; the TOML was edited directly, so the scratchpad generator is no longer the source)
- **INC-0116 (EMFILE on payments-pgbouncer-02):** RB-011 removed. No runbook mentions file descriptors or EMFILE, so it links none.
- **INC-0100, INC-0122:** the card-processor-proxy / `PAYAPI-E1107` symptom is dropped instead of adding a fact (nothing says the proxy resolves the processor endpoint through internal-dns). Both are now prod.internal `SERVFAIL` rows (mechanism `servfail_zone_load`; symptoms ledger-service / auth-service; RB-020; sev2).
- **Runbooks written after their incident can't be linked before it.**
  - RB-016 opens: "I'm writing this down after the May 2026 checkout incident, where we lost the first half hour looking at the wrong service." So it didn't exist before 2026-05-26.
  - RB-013 is the worked example of 2026-01-31.
  - 12 links broke this, all fixed:
    - INC-0015 and 0022 (processor latency) and the five `CPX-W429` blips before 2026-05-26 (0018, 0019, 0048, 0051, 0159) now link no runbook.
    - INC-0029 and 0064 (payments slow cascade) now link RB-010 + RB-014.
    - INC-0020, 0056 and 0077 swap RB-013 for RB-001 (its HikariCP-timeout path).
  - New test `test_no_link_to_a_runbook_written_after_the_incident` covers RB-013, 016, 021, 028 and 030. It fails on the pre-follow-up file (12 violations).
- **RB-014 capped at 15** (was 25), and a test enforces it.
  - Swapped: search GC pauses → RB-003; INC-0114 → RB-002; INC-0181 (2026-07-31) → RB-015 + RB-016.
  - Dropped RB-014 from pgx slow-query rows, INC-0049, 0088, 0003 and 0017.
  - Second links added where the runbook applies, to keep ≥60 multi-runbook rows:
    - zone pushes → RB-020 + RB-027 (a declared hard-negative pair, discriminator `Type=simple`)
    - kafka `/var` rows → RB-008 + RB-009
    - INC-0177 restart-halved budget → RB-029 + RB-023 (RB-023 step 6; discriminator `CPX-E600`)
    - `NTF-E710` rows → RB-009
- **Bounds changed:** rows with no runbook may now number ≤20 (16 do). Real incidents often have no runbook, and here several predate the runbook that would cover them. Hard-negative rows: 14 (cap 15). Rows with ≥2 runbooks: 60, exactly the bound.
- **sessions_failover rows** (INC-0099, 0121, 0143, 0154, 0164) all start after RB-018's review (2025-11-10), so RB-018's "Has this happened before? October 2025" stays true. RB-018 is not edited. Their sidecar facts carry `after_rb018_review = true`, and a test checks the dates.
  - **Prose rule:** these write-ups may say RB-018 was followed, but never that RB-018 records them; RB-018 knows only October 2025.

**Binding for prose and logs** (not stated in any runbook; fixed here and now constraints):
- **INC-0042** ends at **14:09**, as in NOTES. RB-028 has the 504s stopping "within the minute" after the 14:09 cancel; prose may say so, but `resolved_at` stays 14:09.
- **INC-0093** hosts are **ledger-service-01 and ledger-service-02**. No kafka hosts are named because the broker outage's scope isn't stated; prose must not name a broker.
- **Chosen times** (UTC), binding:

  | Incident | Start → end |
  |---|---|
  | INC-0024 | 2025-03-18 15:10 → 16:05 |
  | INC-0055 | 2025-06-24 13:20 → 14:05 |
  | INC-0059 | 2025-07-09 10:40 → 15:30 |
  | INC-0063 | 2025-07-22 08:15 → 11:00 |
  | INC-0070 | 2025-08-12 02:30 → 03:45 |
  | INC-0078 | 2025-09-09 11:20 → 13:05 |
  | INC-0084 | 2025-09-25 04:10 → 07:30 |
  | INC-0087 | 2025-10-07 06:10 → 06:20 |
  | INC-0093 | 2025-10-23 16:40 → 17:55 |
  | INC-0101 | 2025-11-18 05:30 → 06:40 |
  | INC-0108 | 2025-12-09 03:12 → 03:40 |
  | INC-0120 | 2026-01-19 21:00 → 23:30 |
  | INC-0128 | 2026-02-10 09:00 → 15:20 |
  | INC-0133 | 2026-02-23 07:00 → 08:10 |
  | INC-0142 | 2026-03-24 13:30 → 14:15 |
  | INC-0151 | 2026-04-21 10:05 → 10:40 |
  | INC-0157 | 2026-05-12 02:20 → 05:10 |
  | INC-0162 | 2026-05-26 17:05 → 17:50 |
  | INC-0168 | 2026-06-15 19:30 → 2026-06-18 16:00 (the 18 June fix time is chosen) |
  | INC-0174 | 2026-07-07 09:00 → 12:40 |

  INC-0124's `resolved_at` of 03:10 is the batch finish, not when the 503s stopped. All 171 filler times are binding too, as they stand in `skeleton.toml`.

## 2026-10-08: Phase 1 step 3, batch 5 (10 calibration incidents)

**Built:**
- 10 write-ups in `data/incidents/INC-NNNN.md`: 0023, 0048, 0055, 0067, 0099, 0101, 0163, 0168, 0171, 0185. All 10 failure types are covered as the cause type, in 9 formats.
- Front matter is `id`, `title`, `format`, `synthetic`, plus one-line `symptoms` / `root_cause` / `resolution` for the `incidents` table. Every other field stays in the skeleton.
- Log specs in `data/incidents/logs/INC-NNNN.toml`, generated by `scripts/corpus/synthetic_logs.py` (seeded by `rng_for("logs:<id>")`).
- Build validator for write-ups in `scripts/corpus/incidents.py`; log validator in `synthetic_logs.py`. Both run in `build_committed()`.
- New committed outputs, hashed in MANIFEST: `incidents.jsonl` (10) and `log_events.synthetic.jsonl` (798 lines). `incident_windows()` now gives the 10 windows, and 400 LogHub lines are placed with `incident_id` NULL.
- `tests/test_incident_docs.py`: 81 tests.

**Decisions**
- **Choice of ids:** INC-0016 was swapped for INC-0171 (dns_failure was already covered by 0168). INC-0165 was replaced by INC-0067:
  - `LEDGER-W210` (18 or more of 20) can't be reached with parallelism 4.
  - RB-013 puts the connection holders on `recon-worker-*`, so it can't be explained without a new mechanism.
- **Codes are catalog tokens only:** the exact strings in `services.toml` `error_codes`, case-sensitive and not part of a longer token. The HTTP ones (429/502/503/504) count only as bare numbers (`SRCH-E502` holds no `502`). `200`, ms values, ports and counts are free text. Prose and logs may use only the row's codes, and every row code appears in both.
- **Hosts:** prose names only row hosts. A log line may come from any host of the row's service or symptom services (INC-0185 lists only kafka-01/02, but `LEDGER-E455` is logged by ledger-service).
- **Logs are a sample.** Counts in a write-up come from metrics, so the validator doesn't compare counts with log lines.
- **Times:**
  - the start and end clock times must appear
  - an undated time must fall within start − 2 h to end + 2 h
  - timeline lines never go backwards (a drop of more than 12 h means the next day)
  - every "N min" / "N h" must be the total, a gap between two stated times, or on a short whitelist (0171: 23, 168)
- **Anachronisms:** a token from a dated fact (`CPX-W430`, `ndots:2`, `4.12`, `lock_timeout`, …) may appear only if resolved_at + 7 days is on or after that date.
- **Leakage:** word-trigram Jaccard target 0.025 (the test threshold stays 0.03). No 8-word run is shared with a runbook or another write-up (inline code and code blocks excluded). No sidecar keys or mechanism tags appear, except those that are real runbook vocabulary, such as `s_client`.
- **What the number checks prove.** The `facts` and arithmetic tests prove a number is *present* in the write-up and that the arithmetic in the test is right. They don't prove the write-up uses that number correctly (that a 30 is the `cl_waiting` and not something else). Whether the prose is true to the runbooks and facts is reviewed by hand.
- **No migration change.** Ingest (step 5) maps `incidents.jsonl` to `incidents` (with `symptom_services`, `failure_types`, `codes`, `hosts`, `title` and `format` in `metadata`) and `documents`, and the log jsonl to `log_events`. `incidents` is loaded first because of the foreign keys.

**Errors and fixes**
- *Outline review:*
  - 0101's day arithmetic was shown and fixed (20 Oct to 18 Nov is 29 days).
  - 0099 states the primary history and keeps -02 reachable.
  - 0048 lost its retry arithmetic.
  - 0163 was checked against RB-006: RB-006 makes no worker-count claim.
  - 0023 says how the pod was missed.
- *Draft:* trigram Jaccard 0185/RB-027 **0.0563** and 0168/RB-021 **0.0511**, both over the 0.03 threshold; 0055/RB-011 0.0282 and 0023/RB-025 0.0269 over the 0.025 target.
  - Root cause: those runbooks tell the same events, and I reused their phrasing ("a rerun starts at the canary", "has two dots", "asked as written first", "in transaction mode a server connection is tied", "one new pod at a time, three serving throughout"). The 8-word check also caught four of these.
  - Fix: 0185 and 0168 rewritten in their own words, and the flagged sentences in 0055 and 0023 reworded. The thresholds were not raised.
- *Test bugs* (the validator was right each time):
  - A log mutation hit a ledger line, where a ledger host is valid.
  - `has_token` rejected a trailing comma (`10000,`).
  - `s_client` (a real `facts` key of INC-0031) was flagged as a label.
  - INC-0023 has INFO-only logs, which is correct for a TLS failure that never reaches the access log, so the level-mix rule now applies only to rows with codes.

**Found, not changed:** RB-027:62 quotes the Kafka client line as "… could not be established. Broker may not be available." Kafka 3.8 `NetworkClient.java` says "Node may not be available." RB-027 is frozen and not edited, and the synthetic logs don't use that line. Prince to rule.

**Verified:**
- `make corpus` run twice gave a byte-identical MANIFEST
- `make corpus-check` ok
- `make test`: 187 passed (81 new)
- `make lint` clean
- `git diff` on `data/runbooks`, `skeleton.toml` and `eval/` is empty

**Jaccard after the fixes** (top 10, write-up vs anything):

| Pair | Score |
|---|---|
| 0101/RB-008 | 0.0181 |
| 0099/RB-018 | 0.0165 |
| 0055/RB-011 | 0.0149 |
| 0168/RB-021 | 0.0141 |
| 0185/RB-027 | 0.0088 |
| 0163/RB-006 | 0.0081 |
| 0067/RB-022 | 0.0080 |
| 0099/RB-017 | 0.0074 |
| 0067/RB-025 | 0.0068 |
| 0067/RB-026 | 0.0066 |

**Real-world texts used in prose or logs, checked 2026-10-08:**
- systemd `"%s exited, code=%s, status=%i/%s%s"`: systemd `src/core/unit.c`, `unit_log_process_exit` (main).
- Kafka `ConfigException` "Invalid value … for configuration …: Not a number of type INT": batch 3 check.
- `NotEnoughReplicasException` with `acks=all`: batch 1 check.
- Node `getaddrinfo <code> <host>`: run locally (`getaddrinfo ENOTFOUND nonexistent.invalid`).
- Redis Sentinel: the docs list `+sdown` / `+odown` / `switch-master`, and say writes to a partitioned old master "will be lost forever" when it rejoins as a replica, plus `min-replicas-to-write` (redis.io Sentinel docs, latest).
  - The docs use "split brain" only for permanent configuration divergence, so 0099 doesn't use the term.
  - No Sentinel log lines are written: the Sentinels on other VMs belong to services that aren't involved.
- `READONLY You can't write against a read only replica.`: the catalog text (batch 2).

**Unverified:**
- go-redis switching only after `READONLY` on one instance. 0099 says only what RB-018 says (each instance learns on its own).
- Whether OpenSearch's default appender rotates its own logs. Not claimed: "relies on logrotate" is a fiction for our build.
- PgBouncer, Postgres and Envoy log formats were avoided rather than verified.

**Follow-up after review** (tests, validator and NOTES only; no document or runbook changed)
- Arithmetic rows:
  - INC-0048 now checks its stated figure (100 = 400 / 4).
  - INC-0099 computes 18:04:30 + 30 s = 18:05:00, so `hm()` now keeps seconds.
- Anachronism rules are now regex patterns with a date:
  - `WEB_CONCURRENCY` stated as set (`=N`, "set …", "was set"): from 2026-05-29.
  - Edge `/search` limit 250 (near "per pod" / "limit" / "tokens"): from 2026-03-10.
  - Dual-read session encoding: from 2026-07-22.
- New `SUPERSEDED` rule: "`WEB_CONCURRENCY` is/was not set" may not appear in an incident starting after 2026-05-29.
- New `NEVER` rule: `SystemMaxUse` together with "already" on one line. `SystemMaxUse` has no single date (it is applied per VM as RB-008's fix), so a write-up may say it was applied but not that the VM already had it.
- `test_web_concurrency_timeline` checks the rule against rows 0013, 0024, 0030, 0063, 0080 and 0097 (set-claims rejected, "not set" allowed) and 0172 and 0189 ("not set" rejected). Those rows have no write-ups yet; their documents get the same check when written.
- 4 new mutation cases. `make test`: 191 passed.

**Second follow-up after review**
- **Log counters:** `{ramp:a-b}` (bursts only) moves a counter from a to b in time order, and a > b counts down. Applied to:
  - INC-0185: ledger retry queue, retry backoff, outbox pending
  - INC-0171: lag, which rises to 162,000 at 20:30, then falls to 20,000
  - INC-0101: agent backlog %

  The `{int:}` placeholders left are latencies and partition numbers, which really are random per line. Tested: counters never jump back, and a ramp in a single line is an error.
- **INC-0185 logs:** `[KafkaServer id=1] started` at 10:27:05 and `[KafkaServer id=2] started` at 10:27:21. The text was checked against Kafka 3.8 `KafkaServer.scala` (logIdent `[KafkaServer id=N] `, `info("started")`). That class is the ZooKeeper-mode broker, so the brokers run in ZooKeeper mode (new fact). The orders-api backlog line is now a fixed 2410.
- **INC-0171:** the 18:30 line now says it used RB-009's retention check.
- **INC-0099 has the longest session-loss window so far:** logins through auth-service-02 from 18:05 to 18:07:45 (2 min 45 s; the promotion itself was at 18:05:05). RB-018 says the write-failure window "lasts seconds" and doesn't cover a window this long, from a primary cut off from the Sentinels but still reachable by clients. RB-018 is not edited; the write-up doesn't claim RB-018 describes it.

**What to be able to explain in an interview**
- Why the write-ups are checked against structured rows (hosts, codes, times, arithmetic), and what a deterministic validator can't catch (whether a cause is true, voice, implications).
- How seeded, per-item RNG keeps the synthetic logs reproducible and independent.
- Word-trigram Jaccard and shared n-grams as leakage measures, and why copying a runbook into an incident would inflate retrieval scores.
- Why the codes rule is strict: a write-up that mentions a code it isn't about becomes a false hit for exact-token queries.

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
- Brokers run in **ZooKeeper mode** (`KafkaServer`; batch 5 logs use its `[KafkaServer id=N] started` line). Broker ids 1/2/3 = kafka-01/02/03; one data dir `/data/kafka` per broker; tools in `/opt/kafka/bin`; systemd unit `kafka`; broker GC logs in `/var/log/kafka`
- Replication factor 3, `min.insync.replicas=2`; broker default `log.retention.hours=168`
- Topics: `ledger.entries` (**14-day retention**, audit; changes need finance ops' sign-off; **12 partitions**, batch 5), `notifications.outbound` (broker default; temporarily cut to 24 h in RB-009's procedure); consumer group `notification-worker`

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
  - Kubernetes cluster CoreDNS has a `prod.internal` server block with `cache 30` and `forward . 10.0.0.53 10.0.0.54` (default `random` policy). Pods use the Kubernetes default `ndots:5`, **except orders-api, which sets `ndots:2` in its Deployment's `dnsConfig` from 2026-06-18** (RB-021). RB-020's statement stays true for every other pod.
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

**Batch 3 facts** (RB-021 to RB-030)

**orders-api** (RB-021, RB-027)
- Debian-based image (glibc resolver)
- `pg` Pool `idleTimeoutMillis` was the default 10 s; **60000 since 2026-06-18**
- Deployment `dnsConfig` sets ndots "2" since 2026-06-18
- order events are written to an outbox table in orders-db and published by an in-process relay; checkout never waits on Kafka

**Cluster DNS** (RB-022)
- Deployment `coredns` in kube-system, label `k8s-app=kube-dns`
- 2 replicas until 2025-02-20, **4 since**; fixed count, not autoscaled

**card-processor-proxy** (RB-023, RB-029)
- Go service, namespace `payments`
- Client certificate:
  - issued by the processor (not our CA), 1-year validity, CSR through the processor's merchant portal (up to 3 business days; an emergency line exists)
  - Secret `card-processor-client-tls`, read only at start
  - expiry alerts at 30 and 7 days since 2025-01-15; renewed each December
- Processor limit: 400 rps per merchant account
- Local egress limiter **since 2026-07-09**, ConfigMap `card-processor-proxy-config`:
  - `CPX_LOCAL_RPS=180` per instance
  - `CPX_LOCAL_MAX_WAIT=2s`: an attempt queues up to 2 s, goes ahead late if a token frees, otherwise is rejected at 2 s, logs `CPX-W430` and never reaches the processor
  - every attempt, retries included, takes a token
- Pods run only on the 2-node `egress` pool (outbound IPs on the processor's allowlist; nodeSelector plus taint toleration), with required anti-affinity of one proxy per node
- The Deployment uses **maxSurge 0 / maxUnavailable 1**, since a surge pod could never schedule. A restart rolls one instance at a time, so the local budget is halved (180/s) while it rolls (RB-023). If one pod is already Pending, the rollout can't take the running pod down, so it has to be deleted by hand, leaving a gap with no proxy (RB-029).
- A shared cross-instance budget is proposed, **not built**

**auth-db / auth-service TLS** (RB-024)
- auth-db-01 is the primary, auth-db-02 the replica
- auth-service connects as `auth_app` with `sslmode=verify-full` (internal CA)
- `pool_max_conn_lifetime=1h` and `pool_max_conn_idle_time=30m` are set explicitly in its `DATABASE_URL` (the same as the pgxpool defaults)
- Server certificate and key in `/etc/postgresql/16/main/`
- `pg-cert-renew.timer` renews 30 days before expiry, and **since 2025-04-09** also reloads Postgres

**edge-gateway** (RB-025, RB-030)
- Public certificate:
  - issued by a public CA, in Secret `edge-public-tls` (namespace `platform`), mounted as files
  - Envoy reads it at start, so renewal needs `rollout restart`
  - renewed by team-platform 30 days ahead; an outside probe checks expiry daily
- **Only `/search` has an edge local rate limit**:
  - token bucket per edge pod: 100 tokens/1 s until 2026-03-10, **250 since**
  - the rejection is 429 (Envoy's default status for the local rate limit) with body code `EDGE-RL01`. No access-log flag is claimed (see the follow-up below).
  - one bucket per Envoy process (Envoy's default), so one per pod
  - settings in ConfigMap `edge-gateway-envoy`, read at start
  - the limit went in when the route was set up, as a crawler guard
- Growth gives team-catalog a heads-up before large sends

**auth-service sessions** (RB-026)
- Session values in redis-sessions under prefix `sess:`
- Since 2026-07-22, auth-service reads both the old and the July 2026 encodings
- Rollouts use the Deployment defaults (1 surge / 0 unavailable at 2 replicas)

**Rollout tool for VM config** (RB-027)
- Pushes host by host, canary first.
- Before 2026-08-20: a fixed 5-minute wait between hosts; checked only the exit status of `systemctl restart`.
- **Since 2026-08-20, for enrolled units:** wait 60 s, run `systemctl is-active` plus a probe, and stop on the first failure.
- Enrolled units and probes:

  | Unit | Probe |
  |---|---|
  | `kafka` | `kafka-broker-api-versions.sh` |
  | `opensearch` | `_cluster/health` |
  | `redis-server` | `redis-cli PING` |
  | `redis-sentinel` | `redis-cli -p 26379 PING` |
  | `coredns` | `dig @host prod.internal SOA` |
  | `postgresql@16-main` | `pg_isready` |

- **payments-pgbouncer is not enrolled** (not stated in RB-027; see the batch 4 demo constraint in the batch 3 entry).
- Kafka config is at `/opt/kafka/config/server.properties`. The `kafka` unit is `Type=simple`. The under-replicated-partitions alert needs 10 min sustained.

**catalog-api / catalog-db** (RB-028)
- Table `products`; role `catalog_app`
- catalog-api serves with async workers
- Alembic migrations run in the pre-deploy Job `catalog-api-migrate` (namespace `catalog`); the Deployment rolls only if it succeeds
- A weekly `products` export runs as one transaction of about 40 min
- Migrations had no lock timeout before 2025-05-16; **since 2025-05-16** they run with `PGOPTIONS='-c lock_timeout=5s'` and retry
- The edge alerts on the 504 rate
- Rates in May 2025: about 1,200 product reads/s, about 95% from cache (about 60/s to catalog-db), and about 5 writes/s to `products`

**Skeleton rules from batch 3** (each like the 4.12 rule)
- **orders-api (RB-021):** no orders-api incident caused by search-suffix lookup latency may start after 2026-06-18.
- **`CPX-W430` (RB-029):** none may appear before 2026-07-09.
- **Rollout-tool gate (RB-027):**
  - after 2026-08-20, no config-push incident on an enrolled unit may have the tool continue past a failed host
  - units that aren't enrolled (payments-pgbouncer included) are not covered
- **catalog-api migrations (RB-028):** no migration may hold a lock queue longer than about 5 s after 2025-05-16.
- **auth-db certificates (RB-024):** no incident from a renewed but unloaded server certificate after 2025-04-09.
- **card-processor-proxy certificate (RB-023):** no client certificate may expire unnoticed after 2025-01-15.
- **Cluster DNS (RB-022):** no capacity incident on 2 cluster CoreDNS replicas may start after 2025-02-20.
- **Edge `/search` limit (RB-030):** no `EDGE-RL01` incident at 100 per pod may start after 2026-03-10.
- **auth-service sessions (RB-026):** no logout wave from an unreadable session encoding may start after 2026-07-22.

**Batch 4 facts (demo, approved)**
- **F1:** payments-api's db package alternates bouncer hosts (payments-pgbouncer-01/02) on each new connection, with **no fallback (not built)**. Each bouncer carries about half (4 × 40 = 160 client connections, about 80 each). **pgx's own behavior on a failed dial is unverified**, so demo logs may rely only on this fact.
- **F2:** payments-api's logger adds an `errno` field (e.g. `errno=ECONNREFUSED`) when a network error wraps a syscall errno. Go itself prints "connection refused".
- **F3:** the `pgbouncer` systemd unit is `Type=simple`, so `systemctl restart` exits 0 even if pgbouncer dies seconds later. Upstream's shipped unit may differ (unverified).
- **F4:** the rollout tool logs to each host's journal under the identifier `rollout`.
- **F5:** a rollout-tool run can be scoped to a subset of hosts (e.g. a canary-only run).
- Demo: 2026-10-06, a run scoped to payments-pgbouncer-01. The exact config error is **unverified until PgBouncer 1.22 source is read**, and no demo logs are written before then.

**Skeleton rules from batch 4** (approved; tested in `tests/test_incidents.py`)
- R14: no `EDGE-RL01` incident before 2026-03-10 (RB-030, "nobody had looked").
- R15: no other redis-cache primary loss from 2025-12-09 to 2026-01-12 (RB-017, "Last time").
- R16: no other inventory missing-index scan from 2025-07-09 to 2025-08-19 (RB-015, "Last seen").
- R17: no other redis-sessions failover `AUTH-E401X` incident before 2025-11-10 (RB-018).
- R18: no other unrotated-app-log `/var` incident from 2025-11-18 to 2025-12-04 (RB-008, "last time").
- R19: overcommit-fork failures only in 2025-09 (RB-019).
- R20: every Kafka broker-disk incident from 2025-04-11 to 2026-04-11 has `ledger.entries` and `notifications.outbound` as its top two topics, and there are ≥2 of them (RB-009).
- R21: no `CPX-W429` + `ORD-5003` retry storm before 2026-05-26 (RB-016). The 7 `CPX-W429` filler blips carry no `ORD-5003` and have `retry_storm = false`.
- R22: no incident after 2026-09-30; no payments-pgbouncer bad_deploy; no `ECONNREFUSED` row with a payments-pgbouncer host or cause.

**Batch 5 facts** (approved in the batch 5 outline review)
- **journald:** `SystemMaxUse=500M` is applied per VM as part of RB-008's fix; VMs that never had that fix lacked the cap. This covers the 8 `var_journal_growth` rows (0001, 0011, 0079, 0089, 0109, 0127, 0149, 0183), none of which is written yet.
- **search-cluster:**
  - 40 GB root disk, about 11 GB used at base
  - OpenSearch data on its own volume, `/data/opensearch`
  - OpenSearch logs under `/var/log/opensearch/` and relies on logrotate
  - search-cluster-02 was rebuilt 2025-10-20 05:30 without its OpenSearch logrotate file
  - the `/var` 90% alert opens a ticket, not a page; a "logs missing" alert pages
  - the VM log agent ships its own errors over the network
- **redis-sessions primaries:**
  - -01 until 2025-10-07
  - -02 from the planned failover (INC-0087), with no failback
  - -01 again from 2025-11-12 (INC-0099), with -02 a replica since
- **VMs:** run a host firewall managed by team-platform. INC-0099's rule, added by hand, dropped only traffic from the two VMs running the other Sentinels.
- **auth-service:**
  - checks passwords with bcrypt on the request goroutine
  - CPU limit 2 per pod until 2025-08-03, 4 since
  - `GOMAXPROCS` set from the CPU limit in the Deployment
  - returns `503` once more than 64 logins per pod are in flight
  - login route `/v1/auth/login`
  - there is no login rate limit; only `/search` is limited at the edge
- **fraud-scorer:**
  - memory limit 3 GiB (3,072 MiB) per pod, about 150 MiB per warm worker
  - `WEB_CONCURRENCY` **unset until 2026-05-29** (workers = visible CPU count), **16 since**
  - the general node pool has 16-core nodes, plus 32-core nodes since May 2026
  - **Binding:** rows before 2026-05-29 (0013, 0024, 0030, 0063, 0080, 0097) must not say it was set, and INC-0024's fix is the CPU-limit lever. Rows after (0172, 0189) must not describe it as unset.
- **notification-worker:**
  - sends through an external email provider's HTTP API, 20 sends in flight per pod
  - `NTF-E710` fires above a lag of 20,000 messages
  - evening produce rate about 40 messages/s
- **edge-gateway:**
  - certificate renewed 2025-02-14 (`notAfter` 2025-03-16 10:15 UTC)
  - that renewal was applied by deleting pods by hand, and the pod stamped edge-gateway-01 was missed
  - the load balancer spreads new connections evenly over the 3 pods
- **payments:** in June 2025 the evening rate was about 100 payment requests/s.

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
| **2025-01-14** (00:00–02:40 UTC) | RB-023 | **Must exist as an incident.** card-processor-proxy client certificate expired at notAfter 00:00 UTC. `CPX-E600` on both proxy pods; every card payment failed with `PAYAPI-E1107` after 3 attempts. Paged 00:04, emergency certificate from the processor 02:30, Secret update + restart 02:38, clear 02:40. tls_expired. Before RB-023's 2026-09-22 review. |
| **2025-02-20** (19:50–20:15) | RB-022 | **Must exist as an incident.** Cluster CoreDNS (2 replicas) at its CPU limit at evening peak. Lookups timed out in auth-service, catalog-api and orders-api; internal-dns healthy when queried directly. Scaled to 4 at 20:08. dns_failure. |
| **2025-04-08** (09:00–09:53 UTC) | RB-024 | **Must exist as an incident.** auth-db-01 served an expired certificate: renewed file written 2025-03-09, Postgres never reloaded. auth-service new connections failed with `x509: certificate has expired`, logins returned `503`, and older connections aged out. Paged 09:38, `pg_reload_conf()` 09:51; auth-db-02 reloaded too. tls_expired. **No percentage of failing logins is stated.** |
| **2025-05-15** (14:02–14:09) | RB-028 | **Must exist as an incident.** catalog-api migration `ALTER TABLE products` waited behind the weekly export (no `lock_timeout` yet). Cache-miss reads (about 60/s) and writes (about 5/s) on `products` queued and filled the 30 connections. Edge `504` from 14:02:15, QueuePool timeouts from 14:02:30, edge 504-rate alert paged 14:06. ALTER cancelled 14:09; re-run 14:40. bad_deploy / api_latency. **Not** a `CAT-E301` or 53300 incident. |
| **2026-03-10** (09:05–09:50) | RB-030 | **Must exist as an incident.** Growth's spring campaign took `/search` to about 420 rps against the edge limit of 3 × 100. About 120/s (about 29%) got 429 `EDGE-RL01`. search-api flat at about 300/s, no `SRCH-E502`, search-cluster healthy. Limit raised to 250/pod; edge restart 09:46–09:50. rate_limiting. Same month as RB-020's incident, no shared services. |
| **2026-06-15..17** (fixed 2026-06-18) | RB-021 | **Must exist as an incident.** orders-api `/checkout` p99 bursts to about 5 s (some about 10 s, rare `EAI_AGAIN`) over three evenings, from slow lookups of orders-db-01 under ndots:5 (8 queries per lookup) and `pg` idle timeout 10 s. Edge `200`, slow. Fixed with ndots:2 and `idleTimeoutMillis` 60000. dns_failure / api_latency. **No cause claimed for the lost replies.** |
| **2026-07-21** (14:05–14:30) | RB-026 | **Must exist as an incident.** auth-service release changed the session encoding. `AUTH-E401X` from 14:08 (first new pod Ready); page 14:14; rollback 14:20; second wave from 14:23. No `READONLY`, redis-sessions primary unchanged, no data lost (sessions unreadable). Dual-read release 2026-07-22. bad_deploy. |
| **2026-08-13** (10:12–10:36) | RB-027 | **Must exist as an incident.** Rollout-tool push of `message.max.bytes=10MB`: kafka-01 (canary) 10:12 and kafka-02 10:17 both failed to start with `ConfigException`. acks=all writes failed 10:17–10:27 (10 min). Run stopped 10:20 before kafka-03 (due 10:22). ledger-service `LEDGER-E455` with backoff, **no CPU spin, not a retry-spin incident**; orders-api outbox relay backlogged, no `ORD-5003`; notification-worker lag flat, no `NTF-E710`. bad_deploy. |
| **2026-09-16** (19:41–20:23) | RB-029 | **Must exist as an incident.** The egress node under card-processor-proxy-02 was cordoned for a kernel patch; the replacement stayed Pending (allowlisted egress pool + anti-affinity). proxy-01 alone had 180 tokens/s against about 240 payment requests/s: `CPX-W430` and about 60/s `PAYAPI-E1107`. **No `CPX-W429`**, processor healthy, `CPX_MAX_ATTEMPTS` left at 3. Uncordoned 20:20 after the patch and reboot (20:18). rate_limiting. |

## Status

- Phase 0 accounts (AWS, Groq, Gemini, Langfuse Cloud, Anthropic credits, GitHub): **not done**. Prince is doing these in parallel. Repo files only until then.

## Unverified claims (check in Phase 0, record results here)

- AWS: Paid plan vs. Free plan behavior (account closing after 6 months), the $200 credits, the credit-unlock activities.
- Groq: current model list, tool-calling support, free-tier limits.
- Gemini Flash: free-tier limits.
- Langfuse Hobby: monthly unit quota.
