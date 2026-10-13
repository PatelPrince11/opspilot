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
  (Definitions corrected in Phase 2: Recall@k is the mean fraction of gold docs found; the
  at-least-one version is Hit@k. See "Metric definitions, Hit@k".)
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
- **LogHub facts (measured):** 2,000 rows (1,969 INFO, 31 WARNING; `WARNING` → `WARN`), from three source logs (nova-api 1,060, nova-compute 933, nova-scheduler 7), all within 15 minutes on 2017-05-16. So rows are **rebased**: a fixed slice of 10 per incident window (INC-NNNN gets lines (NNNN−1)×10 to NNNN×10, so 200 × 10 uses the 2,000 lines once each; changed from a seeded slice of 40 after batch 6), order kept, `incident_id` NULL. Service names are `openstack-nova-*`, outside `services` (no FK on `log_events.service`). 0 placed so far because there are no incidents yet.
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
- *Severity skew.* The first draw gave sev1 5 and sev3 107 against targets of 16 and 90 (±5). Root cause: the template defaults leaned to sev3. Fixed per template: final 15/57/87/41. (This entry first said 16/56/87/41; the figure changed in the 0100/0122 sev2 follow-up.)
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
- New committed outputs, hashed in MANIFEST: `incidents.jsonl` (10) and `log_events.synthetic.jsonl` (798 lines). `incident_windows()` now gives the 10 windows, and 400 LogHub lines were placed with `incident_id` NULL (40 per window; 10 per window since the change after batch 6, so 100 now).
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
- **Narrative colour is not fact.** Follow-ups, owner names, confirmations (such as a vendor's report) and reported-by details inside write-ups are narrative colour, not Fictional facts. Eval questions must not rely on them.
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

**RB-027 changed after its review:** in commit 3e62118, RB-027:62's Kafka client line changed from "Broker may not be available" to "Node may not be available" (per Kafka 3.8 `NetworkClient`). RB-027 is therefore no longer verbatim from its batch 3 review.

**Found, then fixed in a separate commit:** RB-027:62 quoted the Kafka client line as "… could not be established. Broker may not be available." Kafka 3.8 `NetworkClient.java` says "Node may not be available." Prince approved editing RB-027 for that line only, and it now reads "Node may not be available." Nothing else in RB-027 changed. The synthetic logs still don't use the line.

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
- **INC-0185 logs:** a `systemd: Started kafka.service.` line at 10:27:05 (kafka-01) and 10:27:21 (kafka-02). The orders-api backlog line is now a fixed 2410.
  - At first these were Kafka's own `[KafkaServer id=N] started` lines. That class is the ZooKeeper-mode broker, and nothing in `services.toml` or the facts sets a cluster mode, so the line, not the facts, was changed.
  - Kafka 3.8 has no startup line that is the same in both modes. `Kafka Server started` (`KafkaBroker.STARTED_MESSAGE`) is logged only by `KafkaRaftServer`, which is KRaft.
  - systemd `job.c` logs `Started %s.`. Per `unit.c` `unit_status_string`, `%s` is the unit name when the unit sets no `Description=` (otherwise its description), so the line is exact only for a `kafka` unit with no `Description=`.
  - No cluster-mode fact is recorded.
- **INC-0171:** the 18:30 line now says it used RB-009's retention check.
- **INC-0099 has the longest session-loss window so far:** logins through auth-service-02 from 18:05 to 18:07:45 (2 min 45 s; the promotion itself was at 18:05:05). RB-018 says the write-failure window "lasts seconds" and doesn't cover a window this long, from a primary cut off from the Sentinels but still reachable by clients. RB-018 is not edited; the write-up doesn't claim RB-018 describes it.

**Content pass** (named sentences only, in 0055, 0101, 0099, 0048, 0168 and 0185)
- **0055:** "nothing was lost" removed; the export's share is now "about 90 of the 100 server connections" throughout.
- **0101:** the lost-log window is 05:30 to 06:10 (when OpenSearch lines resumed).
- **0099:** auth-service-02 "moved to the new primary at 18:07:49"; the claim about how it learned is gone.
- **0048:** the processor's next-morning confirmation and "most tried again" removed; "about 180" stays as a stated observation with no derivation.
- **0168:** "written up from this ticket" and the 22:30 comment removed.
- **0185:**
  - "No event was lost anywhere" and "cleared in under a minute" removed.
  - "about 1,200" (retry queue) and "about 2,400" (outbox backlog) match the log spec's ramps (`{ramp:10-1200}`, `{ramp:300-2400}`, backlog line 2410).

**What to be able to explain in an interview**
- Why the write-ups are checked against structured rows (hosts, codes, times, arithmetic), and what a deterministic validator can't catch (whether a cause is true, voice, implications).
- How seeded, per-item RNG keeps the synthetic logs reproducible and independent.
- Word-trigram Jaccard and shared n-grams as leakage measures, and why copying a runbook into an incident would inflate retrieval scores.
- Why the codes rule is strict: a write-up that mentions a code it isn't about becomes a false hit for exact-token queries.

## 2026-10-09: Phase 1 step 3, batch 6 (26 incident write-ups)

**Built:**
- 26 write-ups plus log specs, on branch `batch-6` (WIP checkpoint commits, no final commit):
  - all 8 `/var` journal rows: 0001, 0011, 0079, 0089, 0109, 0127, 0149, 0183
  - all 8 WEB_CONCURRENCY rows: 0013, 0024, 0030, 0063, 0080, 0097, 0172, 0189
  - 10 others: 0015, 0016, 0025, 0031, 0094, 0096, 0152, 0178, 0194, 0197
- Coverage:
  - all 10 failure types as the cause type
  - 4 hard-negative rows: 0016, 0025, 0178, 0197
  - 7 rows with sidecar facts: 0015, 0016, 0024, 0025, 0031, 0178, 0197
  - 3 rows with no runbook: 0015, 0194, 0096
- Formats: 3 each, except handoff with 2 (0134 was dropped).
- Corpus totals: 36 incidents and 2,914 synthetic log lines.
- **Validator rules** (`scripts/corpus/incidents.py`), each with a broken-input test:
  - `BLOCKED_PHRASES` on every write-up: "confirmed the next morning", "sat in the queue", "written up from", "nothing was lost", "no event was lost".
  - `NARRATIVE` (moved here from `tests/test_incidents.py`): a write-up may not cite RB-013/016/021/028/030 for an incident that started before that runbook existed.
  - `NEVER` gains two rules: `SystemMaxUse` stated as set, in place or "had" (beyond the old "already"), and any `WEB_CONCURRENCY=N` with N ≠ 16.
- **Tests:**
  - `BATCH5` (fixed) and `BATCH6` (planned); the tests take the documents present.
  - Per-batch format caps: ≤2 for batch 5, ≤4 for batch 6.
  - Journal rows: every write-up states `SystemMaxUse=500M`, and no host appears in two journal rows.
  - INC-0024 must name the CPU limit.
  - `FORBIDDEN_BY_FACT` gains `internal_dns_healthy` and `data_lost`; every boolean fact needs an entry.
  - Log counters may also count down (`-1`).
  - New ARITHMETIC rows for 0001, 0013, 0015, 0025, 0031, 0080, 0089, 0096, 0097, 0149 and 0197.

**Decisions**
- **Process:**
  - 3 writer subagents drafted in the scratchpad and ran a self-check (validator, logs, facts, labels, blocklist, headings, 8-word runs, Jaccard against everything).
  - The coordinator copied a writer's drafts into `data/` only once the whole set was clean.
  - The gate: `make corpus` ×2 (MANIFEST byte-identical), `corpus-check`, `test`, `lint`.
  - Subagents were authorised for this run only.
- **Skipped rows (QUEUE):**
  - INC-0177: its sidecar discriminator `CPX-E600` is not a row code, so the codes rule rejects any write-up that carries it. The sidecar needs a fix.
  - INC-0134: the draft invented a cause and fix (plan flip, `ANALYZE`); skipped per Prince's ruling.
  - Not chosen: 0042, 0124, 0137 and 0162. Each row's runbook_ids include the runbook written about that incident (RB-028, RB-013, RB-030 and RB-016). **Marked for the next batch:** since 2026-10-09 the validator keeps such post-dated runbooks as labels only, and a write-up must not name them (see the 2026-10-09 entry).
- **Prince's rulings on invented details:**
  - The feature-cache rows (0013, 0063, 0080, 0097) say only that redis-features was slow; RB-006's two sources are named without choosing.
  - Unsupported "nothing changed" / "nobody deployed" sentences were removed (0015, 0096, 0194, 0063, 0030).
  - Ticket-age sentences were reworded (0001, 0127).
  - INC-0152 quotes the OOM text only as the catalog code.
  - In each Jaccard close call, the later document was rewritten; the ceiling was not raised.
- **The 90% `/var` alert "opens a ticket, not a page" is a search-cluster fact only** (batch 5). Write-ups for other VMs say the alert fired or was seen, never how it is routed.
- **`Type=simple` (INC-0025)** appears only as RB-027's statement about the `kafka` unit, used to rule that path out. coredns's unit type stays unstated.
- **LogHub noise budget:**
  - At batch 6 time, 36 windows × 40 lines = 1,440 noise lines were placed from the 2,000-line OpenStack sample. The plan's 37 × 40 = 1,480 assumed 0134.
  - Those were random 40-line slices, so slices overlapped, and past 50 windows the total would exceed the sample.
  - Replaced right after batch 6 (entry below): 10 lines per window, a fixed non-overlapping slice per incident number. The 36 windows now hold 360 lines.
- **Canary check of the reviewers:**
  - 5 planted flaws, in copies of 0197, 0089 and 0015, mixed unlabeled into the review set.
  - Reviewer A (consistency, Opus) caught the host, runbook-date and number flaws.
  - Reviewer B (unsupported claims, Sonnet) caught the invented confirmation.
  - Reviewer C (overlap) caught the copied RB-008 row.
  - No reviewer missed a flaw in its domain, so none was rerun.

**Errors and fixes**
- *First `ExitPlanMode` was rejected with changes,* so plan mode stayed on. Two writers wrote nothing on their first run, and two later stopped on the API rate limit. Fix: the plan was re-approved and the writers were resumed with their drafts intact.
- *`git add` with a bad brace glob* (`INC-00178`) failed, and the first checkpoint commit held only the documents. It was amended with the outputs, the validator and the tests (`ea1b98b`).
- *Facts test `KeyError: 'internal_dns_healthy'`:* the root cause was that `FORBIDDEN_BY_FACT` covered only batch 5's boolean facts. Patterns were added for `internal_dns_healthy` and `data_lost`.
- *Counter tests found 0 lines for 0011, 0079 and 0149 after fix round 1.* The root cause was that W1 reworded "(ticket threshold 90%)" to "(alert threshold 90%)", per the routing decision above. The test regexes were updated; the logs were right.
- *Reviewer findings fixed in round 1:*
  - absolute claims ("nobody outside platform noticed", "users felt nothing", "everybody", "every worker")
  - claims about other teams and their follow-ups
  - 0089's "first half hour" vs 40 minutes
  - near-twins 0024/0030 and 0013/0063 restructured
  - shared closing lists and the "this note won't guess" idiom varied (the idiom now appears in 0013 only)
- *Final fixes (Prince's rulings on the report):*
  - INC-0025 had the bad zone file reach both internal-dns servers, but the row's only internal-dns host is internal-dns-01. The rewrite keeps the push to internal-dns-01 alone: a run scoped to that host (F5), failures intermittent throughout, and the fix pushed there at 15:38. The other server is never named.
  - INC-0172's channel name `#risk-releases` was removed; no fact names it.
- *Reviewer A flagged "namespace `risk`" in 0063 as unbacked,* but RB-006 uses `kubectl -n risk`. A saw no runbooks. W2 had already removed it.

**Verified** (after the final fixes):
- `make corpus` twice gave a byte-identical MANIFEST
- `make corpus-check` ok
- `make test`: 243 passed (46 new)
- `make lint` clean
- `git diff phase-1/scaffold` on runbooks, skeleton, `eval/` and the batch 5 write-ups and log specs shows only INC-0101 (Prince's `6eb3f98`)

**Jaccard after the fixes** (top 10, batch 6 write-up vs anything):

| Pair | Score |
|---|---|
| 0001/0089 | 0.0231 |
| 0001/0109 | 0.0225 |
| 0024/RB-002 | 0.0224 |
| 0109/RB-008 | 0.0216 |
| 0080/0097 | 0.0208 |
| 0013/0063 | 0.0192 |
| 0013/0097 | 0.0188 |
| 0013/0080 | 0.0188 |
| 0011/0109 | 0.0187 |
| 0063/RB-006 | 0.0177 |

No 8-word run is shared with any runbook or write-up. The 6-word runs that remain are host-pair names and short phrases.

**Real-world texts used:** none newly verified. All synthetic log lines are our own services' formats. Error text inside them reuses what runbooks or earlier batches already quote:
- Node `getaddrinfo EAI_AGAIN`
- Python `[Errno -3] Temporary failure in name resolution` (RB-022)
- Go `x509: certificate has expired or is not yet valid` (RB-024)
- the catalog tokens `OOM command not allowed`, `LOADING`, `NXDOMAIN`
- the kubelet OOMKilled line format from INC-0163

**Unverified:**
- Redis's full `OOM command not allowed …` reply wording was not fetched, so only the catalog token is used.

**Open (eval risk, from reviewer C; not fixed in prose):**
- A `/var` journal question that names no host has 8 equally good answers. Eval questions must name the host or date.
- The constraint write-ups (0016/RB-022, 0024/RB-002, 0031/RB-024, 0178/RB-026, 0197/RB-029) retell their runbook's history with the same times. Their questions have two correct documents, and the gold sets must list both.

**What to be able to explain in an interview**
- Why writers drafted outside `data/`, and why each set was gated before it landed: a parallel writer's half-finished file must never break another writer's gate.
- Canary flaws as a test of the reviewers, and why a reviewer is scored only on flaws inside its domain.
- Deterministic rules (blocklist, `NARRATIVE` dates, `NEVER` patterns) vs judgement (absolute claims, near-duplicate incidents), and why the second still needs reviewers.
- Why near-duplicate incidents and runbook retellings are an eval problem (several gold documents), not just a style problem.

## 2026-10-09: LogHub noise, 10 lines per window with no overlap

**Changed:**
- `place_in_windows` now allocates slices sequentially by incident number: INC-NNNN gets lines (NNNN−1)×10 to NNNN×10 of the 2,000-line sample (`PER_WINDOW = 10`).
- 200 incidents × 10 lines = 2,000, so each noise line is used at most once. A window's noise no longer depends on which other incidents have write-ups.
- Timestamps are still seeded random offsets inside the window (`rng_for("loghub:<id>")`). Line order inside a slice is kept, and `incident_id` stays NULL.
- A key past the sample raises `ValueError` instead of wrapping.

**Why:** the old rule took a random 40-line slice per window. Slices overlapped, and with 200 incidents the 2k sample would be reused about four times over. The same OpenStack lines would sit next to many different incidents, which makes noise look like a pattern.

**Tests** (`tests/test_corpus.py`):
- The placement test now uses incident-numbered keys.
- New: slices are sequential and never overlap, and a slot past the sample raises.
- New: `PER_WINDOW × 200 == 2,000`.
- No test asserted 40 directly; the default was 40.

**Verified:**
- `make corpus` twice gave a byte-identical MANIFEST: 36 windows, 360 LogHub lines placed.
- `make corpus-check` ok
- `make test` passed
- `make lint` clean

Committed outputs are unchanged, because LogHub-derived lines aren't committed or hashed. Only `log_events.loghub.jsonl` (gitignored) is regenerated.

## 2026-10-09: LogHub key guard, INC-0025 cache window, INC-0177 sidecar, post-dated runbooks, repo state

**LogHub keys (4fab571):** `place_in_windows` accepts only keys matching `^INC-(\d{4})$` and raises `ValueError("<key>: not an INC-NNNN id")` otherwise.
- Before this, `INC-LIVE-001` took the digits after its last hyphen (`001`) and silently reused INC-0001's slice.
- Broken-input tests: `INC-LIVE-001`, `INC-A`, `INC-00001`.

**INC-0025 (3b3aba6):** the fix was live at 15:38:07, and cluster CoreDNS caches replies for at most 30 s. But the NXDOMAIN burst ran to 15:39:20, longer than the cache allows.
- The burst now ends at 15:38:35, with count unchanged (44 NXDOMAIN lines, 94 lines in all).
- The last NXDOMAIN line is now 2025-03-19T15:38:25.230Z, and the write-up says "the last failed one was logged at 15:38".
- No test pinned the old time or line count.

**INC-0177 sidecar (cd6d740):** the discriminator `CPX-E600` was replaced by `rollout restart`. The old one was not a row code, so the codes rule made the row unwritable. The new token:
- appears in RB-023 step 6 (`k rollout restart deploy/card-processor-proxy`)
- doesn't appear in RB-029, which says "rollout" but never "rollout restart"
- is not a catalog code or a sidecar label word

Only that sidecar entry changed in `eval/`.

**Post-dated runbook links (19dc7b3):** a runbook written about a given incident (NARRATIVE date on or after its `started_at`) stays in that row's `runbook_ids` as a relevance label, but the write-up never names it, including in a header line.
- **An exemption was needed:** the validator required runbook references to equal `runbook_ids`. It now requires them to equal `runbook_ids` minus `postdated(row)`, and the `NARRATIVE` check went from `<` to `<=`.
- **INC-0168** (batch 5) named RB-021, its own post-dated runbook, in its header table. That cell was removed; nothing else in INC-0168 changed.
- Tests: naming RB-021 in INC-0168 fails, and an ordinary missing runbook (RB-020) in the same row still fails.
- Rows 0042, 0124, 0137 and 0162 are marked for the next batch.

**README (f769d00):**
- The disclaimer now covers runbooks, incidents, services and logs, and says the company is fictional.
- The LogHub license notice is copied from `data/raw/loghub/LICENSE` downloaded at `dd61d09` (sha256 matches the pin, `9ffa6ae2…`). Only trailing spaces were stripped.

**CLAUDE.md (045cb2b):** added "Do not use the superpowers plugin's brainstorming, plan-writing, plan-executing or subagent skills. These rules override any plugin skill." No equivalent rule existed.

**Secrets check:**
- `git ls-files` matches only `.env.example` (empty keys, placeholder `change-me`).
- `.env` is gitignored (`.gitignore:2`) and was never committed.
- A pattern scan of tracked files for real keys found none.

**Remote state:**
- `origin` = github.com/PatelPrince11/opspilot, **PUBLIC**.
- `origin/phase-1/scaffold` is at `1a5d32c` (batch 1). Everything later on `phase-1/scaffold` is unpushed.
- `origin/main` is at `0ff6e2b`.

**Eval notes for Phase 1 step 4 (questions):**
- INC-0025 (March 2025, orders-api, internal-dns-01) and RB-020's March 2026 incident (inventory-service, internal-dns-02) are near-twins. Questions must name the year, the server or the service.
- INC-0025's 15:26 page, its 15:31 check and "the diff of the 15:15 push showed the record removed" are narrative colour. No question may rely on them.

**Verified:** step 8 gate below.

## 2026-10-09: Phase 1 step 3, batch 7 (27 incident write-ups)

**Built:**
- 27 write-ups plus log specs:
  - the four post-dated-runbook rows: 0042, 0124, 0137, 0162
  - 0177, which contains "rollout restart"
  - every remaining past-events constraint row: 0004, 0059, 0070, 0078, 0084, 0087, 0093, 0108, 0120, 0128, 0133, 0142, 0151, 0157, 0174
  - balance rows: 0007, 0026, 0034, 0060, 0107, 0125, 0156
- Coverage:
  - all 10 failure types as the cause type
  - formats: 3 each of the 9
  - hard-negative rows: 5 (0042, 0124, 0137, 0162, 0177)
  - no rows without a runbook
- Corpus: 63 incidents, 4,782 synthetic log lines, 630 LogHub lines placed.
- **Validator** (`scripts/corpus/incidents.py`, `checks.py`, `synthetic_logs.py`), each change with a broken-input test:
  - `NARRATIVE` gains `RB-023: 2025-01-14`, because RB-023's samples and History are INC-0004's own night. RB-023 is a label only for 0004, and later rows may name it.
  - `FORBIDDEN_BY_FACT` gains `last_retry_spin`, `data_kafka_involved`, `payments_inventory_extra_db_reads` and `intermittent`. Before this, selfcheck crashed with a KeyError on 0093, 0070, 0108 and 0142. A new test requires every written boolean fact to have a pattern.
  - `BLOCKED_PHRASES` gains statements by outside parties: "hosting provider", "told us it saw", "the processor said" and "the processor told".
  - New `UNVERIFIED_STRINGS` (`+sdown`, `+odown`, `+switch-master`, `Started coredns.service`), checked in prose and in log lines.
- **Tests:**
  - `BATCH7` set and format cap 4
  - 29 ARITHMETIC rows
  - `test_rb023_is_postdated_for_inc_0004_only`
- `scripts/corpus/review_checklist.md` (tracked since the batch 7 cleanup) holds the judgement-only rules; `make packet` includes it.

**Decisions**
- **Process:**
  - 3 Sonnet writers drafted in the scratchpad, after a one-row pilot (0042).
  - A writer's set landed in `data/` only when all 9 drafts passed selfcheck.
  - Reviewers A (Opus, consistency), B (Sonnet, unsupported claims) and C (Sonnet, overlap) worked from `make packet` output.
  - Subagents were authorised for this batch only.
- **Selection rulings:**
  - **0103 deferred.** Its sidecar has only `max_connections_job_leak` and doesn't say what held the about 222 connections beyond orders-api's 75. **0156** (`pgx_slow_queries`) replaced it.
  - **0007 kept.** "Client device clock wrong" needs no expired public certificate; on 2025-01-19 the certificate was valid until 2025-03-16 10:15.
  - **RB-029 is not post-dated.** It is a reference card whose limiter terms date from 2026-07-09; the 2026-09-16 case is an added section. 0177 names it.
  - After the NARRATIVE change, the 36 earlier write-ups were rescanned. Only 0197 (2026-09-16) names RB-023 or RB-029, and that is legal.
- **Format twins:** the twin check (same cause service and cause type, or the same runbook retold) ran against all 63 write-ups. 0142 moved off qa (INC-0025) and 0177 off brief (INC-0048), with 0 collisions after. None of the 8 journal rows is on kafka-02.
- **Sourcing rulings:** these details come from sidecar `facts`, so they were kept:
  - 0042 export start 13:30
  - 0124 batch start 23:30
  - 0004 3.5/s per pod
  - 0084/0174 caught by the 50 GB alert
- **0142:**
  - The prose says only that the zone file reached internal-dns-02 alone. No scoped run and no explanation of why.
  - Sidecar `missing_record = inventory-db-01.prod.internal` is paraphrased ("the inventory database record"): inventory-db-01 is not a row host, so the host rule blocks the name. Accepted rather than editing the skeleton.
- **0093:** no "4.12" (anachronism: resolved + 7 days < 2025-11-12); backoff appears only as an undated follow-up.
- **Grace-window tokens** (`lock_timeout` in 0042, 250 per pod in 0137) appear only as follow-ups.
- **0128:** the fix (a size cap on the response cache) is inferred from RB-005's "no size cap". The packet gives no fix.

**Errors and fixes**
- *Selfcheck KeyError on boolean facts* (0093, then 0070/0108/0142).
  - Root cause: `fact_errors` looks up every boolean fact in `FORBIDDEN_BY_FACT`, and only batch 5/6 keys had entries.
  - Two writers worked around it with local wrappers (since deleted).
  - Fixed by adding the patterns plus a test that covers every written row.
- *Rate limit:* W3 stopped on the session limit (about 15:10 to 19:00) after 8 drafts. It resumed from `progress.toml` with its drafts intact and wrote 0133.
- *Reviewer A couldn't write its findings file* (the harness told subagents to return text); the coordinator saved it.
- *Canaries:* 5 flaws planted in copies (0004 time order, 0151 2×80=140, 0108 provider statement, 0177 processor statement, 0084 RB-007 paraphrase).
  - A caught 0004 and 0151, B caught 0108 and 0177, C caught 0084. B also caught A's two.
  - No reviewer missed a canary in its domain, so none was rerun.
- *Review: 131 findings* (A 21, B 42, C 68). Main classes:
  - **C, 19 must-fix:** structural retelling of a runbook in its own order (steps, FAQ, table rows, decision tree), invisible to Jaccard and the 8-word check. This includes retelling label-only runbooks in 0042, 0124, 0137 and 0162.
  - **B:** invented check results, causes and fixes; absolute claims; unverified log formats (Sentinel event lines, systemd `Started coredns.service.`).
  - **A:**
    - effect before cause (0177's restart time)
    - mid-incident arithmetic that uses the end time (0128)
    - logs crossing thresholds before the alert (0070)
    - in-process cache not reset by OOMKills (0128)
    - reloads 14 s apart under the pre-2026-08-20 5-min pacing (0142)
    - Team field (0128)
  - Fixed in one round. Script-catchable classes became the rules above; the rest went to the checklist.

**Verified:**
- `make corpus` twice gave a byte-identical MANIFEST
- `make corpus-check` ok
- `make test`: 298 passed (40 new)
- `make lint` clean
- `git diff phase-1/scaffold` on runbooks, skeleton and `eval/` is empty
- none of the 36 earlier write-ups or log specs changed

**Jaccard after the fixes** (top 10, batch 7 write-up vs anything):

| Pair | Score |
|---|---|
| 0042/RB-028 | 0.0225 |
| 0108/RB-017 | 0.0224 |
| 0162/0048 | 0.0205 |
| 0137/RB-030 | 0.0198 |
| 0124/RB-013 | 0.0192 |
| 0004/0048 | 0.0189 |
| 0142/RB-020 | 0.0187 |
| 0026/RB-011 | 0.0186 |
| 0059/RB-015 | 0.0173 |
| 0070/0089 | 0.0156 |

No 8-word run is shared with any runbook or write-up.

**Real-world texts verified 2026-10-09:**
- `SENTINEL FAILOVER <master name>`: redis.io Sentinel docs (latest), https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/ ("Force a failover as if the master was not reachable, and without asking for agreement to other Sentinels"). Used in 0087.
- `Can't save in background: fork: Cannot allocate memory`: Redis **7.2.4** `src/rdb.c:1583`, `serverLog(LL_WARNING,"Can't save in background: fork: %s", strerror(errno))`, https://raw.githubusercontent.com/redis/redis/7.2.4/src/rdb.c; the ENOMEM text "Cannot allocate memory" comes from glibc `sysdeps/gnu/errlist.h` (master). Used in 0078.
- `Background saving started by pid %ld`: same file, `rdb.c:1587`. Not used verbatim.

**Unverified (kept out of documents and logs):**
- Go's TLS alert text for an expired client certificate
- Sentinel event log-line formats
- systemd `Started coredns.service.` (depends on the unit's `Description=`)
- Postgres PANIC wording beyond what RB-007 quotes

**Eval notes (for Phase 1 step 4):**
- **Rows that retell their runbook's history.** Gold sets need both documents:
  - 0004/RB-023
  - 0042/RB-028
  - 0059/RB-015
  - 0070/RB-008
  - 0078/RB-019
  - 0084, 0157, 0174/RB-007
  - 0087/RB-018
  - 0093/RB-001
  - 0108/RB-017
  - 0120/RB-009
  - 0124/RB-013
  - 0128 and 0133/RB-005
  - 0137/RB-030
  - 0142/RB-020
  - 0151/RB-012
  - 0162/RB-016
- **Twin pairs, with what tells them apart:**
  - 0007/0023: client clock on edge-gateway-03, 2025-01-19, vs missed pod edge-gateway-01, 2025-03-16
  - 0125/0016: internal-dns-01 not answering, VMs, 2026 vs cluster CoreDNS CPU, pods, 2025
  - 0125/0025: no answer vs NXDOMAIN; kafka-02 vs orders-api; 2026 vs 2025
  - 0125/0142: internal-dns-01 silent vs zone file on internal-dns-02
  - 0142/0016: zone file on internal-dns-02 vs cluster CoreDNS CPU
  - 0142/0025: internal-dns-02, inventory-service, 2026 vs internal-dns-01, orders-api, 2025
  - 0162/0048: CPX-W429 retry storm, sev1, 2026-05-26 vs a 5-minute blip in 2025
  - 0177/0048: CPX-W430 during a rollout restart vs CPX-W429 blip
  - 0162/0177: CPX-W429 + ORD-5003 vs CPX-W430 only
  - 0162/0197: processor slow, CPX-W429 vs node cordon, CPX-W430
  - 0177/0197: 5-min rollout restart vs 42-min cordon
  - 0156/0055: payments-api slow queries, 2026 vs PgBouncer export, 2025
  - 0059/0151: missing index, INV-E220, 2025 vs pool 20→80, 53300, 2026
  - 0070/0089: kafka-02 GC logs vs kafka-01 journal
  - 0070/0149: kafka-02 GC logs vs kafka-03 journal
  - 0070/0120: `/var` vs the data disk
  - 0070/0101: both retell RB-008; kafka vs search-cluster
  - 0120/0089 and 0120/0149: kafka-03 data disk, 2026-01 vs `/var` journal rows
  - 0078/0152: overcommit 2, MISCONF, 2025-09 vs a 2026-04 row
  - 0084/0157/0174: `cdc_payments` slot, 2025-09 vs archiving failure, ENOSPC, 2026-05 vs `payments_db_02` slot, 2026-07
  - 0087/0099: planned failover, auth-service-01, 2025-10-07 vs partitioned primary, auth-service-02, 2025-11-12
  - 0108/0094: VM lost, catalog-db-01 CPU, 2025-12-09 vs 2025-10-26 row
  - 0128/0133: catalog-api-02 every 50 min vs notification-worker-01 Monday batch

**What to be able to explain in an interview**
- Why a runbook retold in its own order is leakage even when Jaccard is under 0.025, and why that took a reviewer, not a script.
- Post-dated runbooks: a relevance label without a citation, and how `NARRATIVE` dates enforce it.
- Canary flaws scored per reviewer domain, and what a cross-domain catch (B catching A's flaws) does and doesn't prove.
- Turning review findings into deterministic rules (blocklists, unverified strings, boolean-fact patterns) and leaving the rest as a checklist.

**Cleanup after batch 7** (2026-10-09)
- **INC-0007:** the prose said the client's connections "end up on edge-gateway-03". With an even load-balancer spread, a wrong client clock fails against every pod. Rewritten strictly from the sidecar (client device clock wrong): edge-gateway-03 is named only because its log has the broken-off handshake and its certificate was read. An Opus reviewer's findings (the unchecked "same certificate on all pods" inference, the handshake treated as the client's confirmed attempt, absolute claims) were fixed.
- **INC-0128:** RB-005 states the cause (a cache with no size cap) but not the fix. The resolution now says only who took it (catalog on-call) and that no OOMKill was logged after 14:50, with resolution at 15:20. Also fixed: the 11:00 comment named a 10:10 restart; the log's restarts are at 09:50 and 10:40.

**Eval notes (batch 7, added in cleanup):**
- The resolutions of 0060, 0107, 0125 and 0128 are not stated in any source, so no question may ask what fixed them.
- 0142's missing record is paraphrased ("the inventory database record"), so no question may use its hostname.
- 0107, 0125 and 0133 are short briefs, so questions on them must be host, code or date lookups.

**Status:**
- 63 of 200 incidents written.
- INC-0103 and INC-0134 now have approved sidecar facts (see "Batch 7 cleanup facts") and are writable in batch 8. Neither needed a new host, service, code or skeleton change.

**Rules for future batches:**
- A writer never infers a fix from a cause. With no sourced fix, the resolution states only who took the incident and when the errors stopped.
- Any runbook that retells a row's event as the reason it was written gets a `NARRATIVE` date, and is then a label only for that row (as RB-013, 016, 021, 023, 028 and 030 are).

## 2026-10-10: Phase 1 step 3, batch 8 (27 incident write-ups)

**Built:**
- 27 write-ups plus log specs:
  - writable deferrals: 0103, 0134 (batch 7 cleanup facts)
  - every unwritten hard-negative row: 0033, 0038, 0081 (RB-020/RB-027)
  - balance rows: 0005, 0022, 0028, 0046, 0049, 0061, 0065, 0068, 0073, 0074, 0077, 0086, 0091, 0095,
    0110, 0119, 0123, 0145, 0155, 0167, 0186, 0195
- Coverage: cause type written conn_pool 9, tls 9, api_latency 9, rate_limiting 9, dns 8, high_cpu 8,
  redis_down 8, memory_leak 8, bad_deploy 8, disk_full 14. Formats 3 each of the 9. Rows with no
  runbook: 0022, 0134.
- Corpus: 90 incidents, 6,568 synthetic log lines, 900 LogHub lines placed.
- **Validator** (`scripts/corpus/incidents.py`, `checks.py`), each with a broken-input test:
  - `FORBIDDEN_BY_FACT` gains `limiter` (0028: the edge /search limit was not involved).
  - `BLOCKED_PHRASES` gains meta-talk and outside confirmations: "this document", "this write-up",
    "the sources do not", "does not guess", "on its behalf", "notes from the night", "status page".
  - `BLOCKED_EXEMPT`: four earlier write-ups break the new phrases and are exempt by name, not edited
    (open item below).
  - `UNVERIFIED_STRINGS` gains "dataset is being loaded" (Redis's LOADING sentence; only the token is verified).
  - Anachronisms: a "Month YYYY" history reference later than the write-up date (resolved + 7 days) fails.
    ISO dates are not checked: a certificate `notAfter` (0007) is a legitimate future date.
- **Tests:** `BATCH8` set and format cap 4; 37 ARITHMETIC rows; 346 tests.
- `scripts/corpus/review_checklist.md`: batch 8 judgement rules appended.

**Decisions**
- **Process:** same as batch 7 (3 Sonnet writers split by domain, one-row pilot 0081 read before the
  others launched, a writer's set landed only when all of it passed, reviewers A Opus / B, C Sonnet on
  `make packet` output). Subagents authorised by Prince for this run.
- **Severity is balanced against `skeleton.toml`, which is 15/57/87/41.** The "16/56/87/41" in the batch 4
  entry predates the 0100/0122 sev2 follow-up. Batch 8 added 3/7/11/6.
- **Screened out before selection** (so not deferrals): sessions_failover rows 0121/0143/0154/0164 (no fact
  says why or which way the primary moved, and "-02 a replica since 2025-11-12" would flip); redis-cache
  primary losses 0041/0062/0090 (each needs an unrecorded failback); 0153 (ECONNREFUSED near the demo);
  0116 (fd limit unstated); max_connections_adhoc rows (who held the slots, the 0103 gap); 0113 (a roll
  hits both pods, so -02 being spared would be invented); 0102 (what filled the disk; R18 rules out the
  unrotated log).
- **No post-dated runbooks.** No row starts on or before a NARRATIVE date of its runbooks, and no linked
  runbook's dated history line is one of these events. `NARRATIVE` unchanged.
- **Fixes:** only 0103 and 0134 have sourced fixes. Every other resolution names who took it and when the
  errors stopped. A runbook's remedy for the case may be cited as the runbook's (0068, 0081), never as applied.
- **DNS trio (0033/0038/0081):** the bad zone file is named on the row's one internal-dns host only;
  no scoped run, no word about the other server. `Type=simple` appears only as RB-027's statement about
  the `kafka` unit. Per-host rollout log lines don't say "run complete" (pre-2026-08-20 pacing would then
  imply the other server got the file first).
- **Log hosts:** reviewer B flagged log lines from symptom-service hosts that aren't row hosts. Rejected:
  the batch 5 rule allows any host of the row's service or symptom services. Reviewer A agreed.
- **Owner teams:** 0073 (team-dba) and 0110 (team-payments) name the linked runbook's owner as the team
  that took the incident. Narrative colour, not a fact.

**Errors and fixes**
- *Coordinator: packets for 0068 and 0086 were never generated.* Root cause: I swapped them in for
  0043/0113 after generating packets. W2 drafted from the skeleton, sidecar and RB-025; the packets were
  generated before review and the stale 0043/0113 packets removed.
- *Coordinator: rows.md named fraud-scorer-02 for 0074;* the row says fraud-scorer-01. The writer
  followed the skeleton.
- *Heading collision 0038/0119* (both tickets, same writer) failed `test_headings_never_repeat` after
  landing; 0119's headings renamed, later rebuilt in the fix round.
- *Machine sleep* killed the writers four times (one pilot, three sets). Keep-awake doesn't cover a closed
  lid. Resumed from `progress.toml` with drafts intact; nothing was redone.
- *Pilot 0081:* talk about "the sources", rollout pacing that implied the other server got the file, and
  0025's closing list. Fixed by the coordinator; the lessons went into the brief before the others started.
- *Canaries:* 5 planted (0049 2×20=60, 0110 errors from 21:31 before the 21:37 start, 0022 the processor's
  status page "confirmed", 0065 an invented VACUUM FULL, 0123 RB-019 walked top to bottom). A caught its
  two, B its two, C its one; each also caught the others'. No rerun.
- *Review: 202 findings* (A 28, B 118, C 56). Main classes: invented check results, check times and
  outside reports (B); runbooks retold in order: RB-003 in 0028, the RB-020 remedy steps in all three DNS
  rows, RB-019 in 0155, RB-024's Q&A in 0073 (C); stock idioms across the batch (C); log ramps past
  their trigger (0046), 503s before the timeouts that cause them (0077), July 2025 cited in a June 2025
  incident (0049) (A). Fixed in one round. Script-catchable classes became the rules above.
- *Gate, first run:* `make corpus` failed: INC-0089 (batch 6) says "this write-up". Root cause: my
  pre-rule scan flagged it but the line was cut off in my output and I missed it in the exempt list.
  Added and reported. Then one test failed: the "July 2025" mutation on INC-0055 is legal (its write-up
  date is 2025-07-01); the test case moved to "August 2025". The validator was right.

**Verified:**
- `make corpus` twice gave a byte-identical MANIFEST
- `make corpus-check` ok
- `make test`: 346 passed
- `make lint` clean
- `git diff phase-1/scaffold` on runbooks, skeleton and `eval/` is empty; only the 27 batch 8 write-ups
  and log specs changed under `data/incidents/`

**Jaccard after the fixes** (top 10, batch 8 write-up vs anything):

| Pair | Score |
|---|---|
| 0186/0177 | 0.0224 |
| 0195/0162 | 0.0207 |
| 0145/0107 | 0.0207 |
| 0186/0048 | 0.0200 |
| 0186/0110 | 0.0197 |
| 0081/0033 | 0.0196 |
| 0155/0152 | 0.0194 |
| 0081/0025 | 0.0183 |
| 0081/0038 | 0.0182 |
| 0119/0060 | 0.0182 |

No 8-word run is shared with any runbook or write-up.

**Real-world texts:** none newly verified. Used only what NOTES or a linked runbook already quotes:
HikariCP `request timed out after 30000ms.`, SQLAlchemy QueuePool text, PgBouncer `pooler error:
query_wait_timeout` / `08P01`, RB-012's 53300 message, RB-025's openssl verify codes 10 and 21, catalog
tokens `OOM command not allowed` and `LOADING`.

**Unverified (kept out):** Redis's full LOADING and OOM replies; Go x509 texts other than RB-023/RB-024
quotes; OpenSearch rejection exception text; Postgres lock-wait log wording.

**Eval notes (for Phase 1 step 4):**
- **Rows that retell a runbook's history:** none in batch 8.
- **Twin pairs, with what tells them apart:**
  - 0081/0033/0038 and 0025/0142: zone push NXDOMAIN. 0033 auth-service-01, internal-dns-02, 2025-04-21;
    0038 ledger-service-01, internal-dns-01, 2025-05-02; 0081 catalog-api-02, internal-dns-02, 2025-09-19;
    0025 orders-api-02, internal-dns-01, 2025-03-19; 0142 inventory-service, internal-dns-02, 2026-03-24
  - 0068/0086/0061 and 0007/0023 (edge TLS): intermediate expired, all pods, sev1, 2025-08-07 vs
    intermediate missing, 2025-10-06 vs client clock on edge-gateway-01, 2025-07-17 vs client clock,
    edge-gateway-03, 2025-01-19 vs missed pod, 2025-03-16
  - 0110/0004: processor-side CA rotation, 2025-12-19 vs our client certificate expired, 2025-01-14
  - 0073/0031: auth-db certificate name mismatch, 2025-08-21 vs renewed but unloaded, 2025-04-08
  - 0186/0195/0048/0162/0177/0197: CPX-W430 from both pods (traffic above 360), 2026-08-13 vs CPX-W429
    blip 2026-09-12 vs 2025 blip vs retry storm vs rollout restart vs cordon
  - 0167/0137: crawler at 250/pod, 2026-06-13 vs campaign at 100/pod, 2026-03-10
  - 0119/0060: edge-gateway-03 node pressure, 2026-01-16 vs the earlier edge latency row
  - 0155/0152 and 0078/0123: redis-features maxmemory 2026-05-03 vs 2026-04-26; overcommit 2025-09;
    LOADING after a restart 2026-01-25
  - 0095/0055/0156: payments PgBouncer long transactions with query_wait_timeout, 2025-10-27 vs refunds
    export, 2025-06-24 vs pgx slow queries, 2026
  - 0077/0124: ledger Hikari slow queries, 2025-09-08 vs month-end reconciliation, 2026-01-31
  - 0022/0015: processor latency, 2025-03-15 vs the earlier processor-latency row
  - 0145/0107: search-cluster-02 segment merges vs 0107's row (same shape, different cause)
- **Unanswerable resolutions:** every batch 8 row except 0103 and 0134. No question may ask what fixed
  them.
- **Short documents:** briefs 0061 (147 words), 0065 (136), 0145 (108): host, code or date lookups only.

**Deferrals:** none.

**Open (needs Prince):**
- Four earlier write-ups break the new phrase rules and are exempt by name, not edited: 0034, 0089 and
  0125 ("this write-up", meta-talk); **0171 (batch 5) states an outside party's statement** ("the
  provider's status page acknowledges degraded API latency"), the class blocked since batch 7.

**Status:** 90 of 200 incidents written.

**What to be able to explain in an interview**
- Why a pilot document is read before the other writers start: its mistakes become brief rules once,
  instead of 26 review findings.
- Why a new rule that hits old documents is exempted by name and reported, not silently relaxed or
  silently applied.
- Why the coordinator screens rows for facts a write-up would have to invent before selecting them.

## 2026-10-11: Phase 1 step 3, batch 9 (27 incident write-ups)

**Built:**
- 27 write-ups plus log specs, selected after batch 8 landed: 0006, 0008, 0014, 0035, 0037, 0039, 0043,
  0054, 0069, 0076, 0082, 0083, 0088, 0112, 0117, 0126, 0136, 0146, 0159, 0165, 0166, 0169, 0179, 0182,
  0184, 0187, 0196
- Selection: no writable deferrals and no unwritten hard-negative rows were left, so the batch is pure
  balance: +3 for each of the 9 non-disk cause types. Severity +2/8/10/7. Formats 3 each. Rows with no
  runbook: 0159, 0182.
- Corpus: 117 incidents, 8,439 synthetic log lines, 1,170 LogHub lines placed.
- **Validator**, each with a broken-input test:
  - `FORBIDDEN_BY_FACT` gains `release_delayed` (0112: never "went out on time" / "not delayed").
  - `BLOCKED_PHRASES` gains "the row is filed" and "the row for this incident" (talk about the
    skeleton row). "this row" stays allowed: 0068/0086 use it for RB-025's table row.
  - `UNVERIFIED_STRINGS` gains "sentinel: failover" (a Sentinel event line reworded is still one).
- **Tests:** `BATCH9` set and cap 4; 32 ARITHMETIC rows; 383 tests.
- `review_checklist.md`: batch 9 judgement rules appended.

**Decisions**
- **Process:** as batch 8, with fresh writers (pilot 0043 read before the others launched), fresh
  reviewers and new canaries.
- **No post-dated runbooks.** RB-013 (0136), RB-021 (0179, 0196), RB-028 (0112) and RB-030 (0146) all
  predate their rows; no linked runbook's dated history line is one of these events. `NARRATIVE` unchanged.
- **No fixes are sourced** in batch 9: every resolution names who took it (a runbook's owner team, or
  "the on-call") and when the errors stopped.
- **RB-021 rows 0179/0196** rest on RB-021's statement that only orders-api sets its own ndots; neither
  claims a resolver library, the 8-query figure or retry counts.
- **0054** keeps redis-cache's primary history unstated (the reason redis-cache primary-loss rows were
  screened out). `SENTINEL FAILOVER <master name>` is cited as the documented way, not as what was typed.
- **Log samples** no longer carry per-minute counts, query ages or a dozen kill lines when the prose says
  the count wasn't recorded (review finding class; checklist rule).

**Errors and fixes**
- *Usage limit* stopped all three writers (about 01:30 to 10:20 MDT on 2026-10-11) with 16 of 27 drafts
  passing; resumed from `progress.toml`, nothing redone.
- *Pilot 0043:* invented "until clients complained", an absolute about monitoring, "the record", and a
  renewal trigger nobody recorded. The coordinator's fix pushed Jaccard vs 0068 to 0.0254; the writer
  restructured it. Coordinator error: I marked 0043 selfchecked in `progress.toml` before the rewrite
  passed (it did pass before landing).
- *Canaries:* 0035 4×40=120, 0126 a rejected connection at 08:51 before the 09:00 start, 0159 the
  processor's account team blaming maintenance, 0169 team-risk deleting keys at 12:55, 0179 RB-021 walked
  in order. A caught its two, B its two, C its one; each also caught others. No rerun.
- *Review: 149 findings* (A 37, B 70, C 42). Main classes: log samples implying unrecorded counts, and
  errors before their ramp's ceiling (A); invented reasoning, check times, "the row" meta-talk (B);
  mechanism explanations repeated across siblings (event loop 0037/0166, ndots 0179/0196, the
  redis-features trio's argument order), runbook retellings, 0182's nod to "the earlier orders-api pool
  incident" (C). A also found 0054 logging a Sentinel event from redis-cache-01, which runs no Sentinel.
  Fixed in one round; 0054 dropped below its word minimum after the coordinator cut an inference and got
  a sourced Sentinel-quorum sentence.
- *Gate:* passed on the first run.

**Verified:**
- `make corpus` twice gave a byte-identical MANIFEST
- `make corpus-check` ok
- `make test`: 383 passed
- `make lint` clean
- `git diff phase-1/scaffold` on runbooks, skeleton and `eval/` is empty; only the 27 batch 9
  write-ups and log specs changed under `data/incidents/`

**Jaccard after the fixes** (top 10, batch 9 write-up vs anything):

| Pair | Score |
|---|---|
| 0136/0077 | 0.0240 |
| 0126/0073 | 0.0233 |
| 0112/0091 | 0.0228 |
| 0054/RB-017 | 0.0211 |
| 0146/0028 | 0.0210 |
| 0146/0014 | 0.0202 |
| 0035/0083 | 0.0196 |
| 0043/0068 | 0.0195 |
| 0076/RB-020 | 0.0185 |
| 0184/0152 | 0.0181 |

No 8-word run is shared with any runbook or write-up.

**Real-world texts:** none newly verified. Used: RB-025's openssl verify code 21, `SENTINEL FAILOVER
<master name>` (batch 7), RB-028's `PGOPTIONS='-c lock_timeout=5s'`, HikariCP's `request timed out after
30000ms.`, catalog tokens. **Unverified (kept out):** Sentinel and CoreDNS log lines, Go x509
name-mismatch text, `getaddrinfo EAI_AGAIN` for services whose runtime isn't stated, Postgres lock-timeout
error text, Alembic output.

**Eval notes (for Phase 1 step 4):**
- **Rows that retell a runbook's history:** none in batch 9.
- **Twin pairs, with what tells them apart:**
  - 0043/0086: intermediate missing on all three edge pods, sev1, 2025-05-15 vs sev3, 2025-10-06
  - 0082/0007/0061: client clock, edge-gateway-03 2025-09-21 vs edge-gateway-03 2025-01-19 vs
    edge-gateway-01 2025-07-17. **0082/0007 share host and cause: questions must name the date.**
  - 0126/0073: auth-db name mismatch, sev2 2026-02-08 vs sev3 2025-08-21
  - 0035/0156/0055/0095: payments pool, 2025-04-24 slow queries on -03/-04 vs 2026 slow queries vs
    PgBouncer export vs long transactions with query_wait_timeout
  - 0083/0035: release regression filling pools on -01/-03, 2025-09-23 vs slow queries on -03/-04
  - 0136/0077/0124: ledger Hikari slow queries, ledger-service-02 2026-03-08 vs -01 2025-09-08 vs month-end
  - 0182/0134: orders-api pool, 2026-08-08 -02/-03 vs month-end order summaries 2026-02-28 -01/-03
  - 0169/0184/0155/0152: redis-features maxmemory, 2026-06-17 vs 2026-08-11 vs 2026-05-03 vs 2026-04-26
  - 0146/0014/0028: search-cluster pool rejections, campaign 2026-04-15 vs 2025-02-19 (-01/-03) vs
    2025-03-26 (-02/-03)
  - 0187/0107: search-cluster GC pauses vs 0107's row
  - 0159 and the six card-processor-proxy rate rows: a 10-minute CPX-W429 blip on 2026-05-19, no PAYAPI-E1107
  - 0179/0196: ndots search-list latency, notification-worker 2026-07-23 vs catalog-api 2026-09-15
  - 0076 and the internal-dns rows: SERVFAIL (zone not loaded) on internal-dns-01, 2025-09-02
  - 0054/0108/0094: planned redis-cache failover 2025-06-19 vs VM loss 2025-12-09 vs 0094's row
  - 0165/0093: ledger-service-01 month-end serialization CPU 2026-06-01 vs retry spin 2025-10
  - 0006/0133: notification-worker-01 template cache 2025-01-18 vs Monday batch 2026-02
  - 0037/0166: orders-api CPU-bound event loop, -02/-03 2025-04-29 vs crypto on -01 2026-06-09
  - 0069/0039: OOMKilled, orders-api-03 Node heap vs auth-service-01 goroutines
- **Unanswerable resolutions:** all 27. No question may ask what fixed a batch 9 incident.
- **Short documents:** briefs 0014 (106 words), 0037 (117), 0117 (164): host, code or date lookups only.

**Deferrals:** none.

**Status:** 117 of 200 incidents written (see the scope cut below).

**What to be able to explain in an interview**
- Why canaries change every wave: a reviewer that saw last wave's flaws could match on them.
- Why synthetic log samples can leak claims the prose avoids (counts, durations), and how the review
  caught it.
- Why one sibling document "owns" a shared mechanism explanation: retrieval would otherwise see several
  near-identical passages for one question.

## Scope cut 2026-10-11

**Decision:** the incident corpus stops at **117 written incidents** (batches 5 to 9). The 83 unwritten
skeleton rows **stay in `data/incidents/skeleton.toml`** (and their sidecar entries in `eval/`), so ids,
the skeleton tests and the constraint rows don't change, but they **get no document and are never
ingested**: step 5's `make ingest` loads only rows that have a write-up (the same set `incidents.jsonl`
already holds), and no eval question may target an unwritten row.

**Reason:** time. Each 27-row batch costs a full write/review/fix/gate cycle, and Phase 1 still needs the
eval set (step 4) and ingestion (step 5). 117 incidents plus 30 runbooks (147 documents) keeps the corpus
large enough for the Phase 2 retrieval ablation (dense vs keyword vs hybrid vs reranked), with every cause
type at 11 or more incidents.

**Final written counts**

| Cause type | Written | Skeleton |
|---|---|---|
| api_latency | 12 | 23 |
| bad_deploy | 11 | 25 |
| conn_pool_exhaustion | 12 | 22 |
| disk_full | 14 | 20 |
| dns_failure | 11 | 20 |
| high_cpu | 11 | 20 |
| memory_leak | 11 | 20 |
| rate_limiting | 12 | 16 |
| redis_down | 11 | 20 |
| tls_expired | 12 | 14 |
| **total** | **117** | **200** |

| Severity | Written | Skeleton |
|---|---|---|
| sev1 | 9 | 15 |
| sev2 | 33 | 57 |
| sev3 | 51 | 87 |
| sev4 | 24 | 41 |

Formats: postmortem 14, handoff 12, the other seven 13 each. Rows with no runbook: 8. Hard-negative
rows: all 14 are written. Every past-events constraint row is written.

**Consequences for later steps**
- Step 4 (questions): gold sets may cite only the 117 written incidents and the 30 runbooks. The
  per-batch eval notes (twins, retelling rows, unanswerable resolutions, short documents) apply.
- LogHub: 117 windows × 10 lines = 1,170 of the 2,000 lines are placed; the rest stay unused.
- The skeleton's balance tests still describe all 200 rows; nothing claims the written subset matches
  them beyond the counts above.

## 2026-10-11: PgBouncer 1.22 startup failure, verified from source

**Source:** https://github.com/pgbouncer/pgbouncer at tag `pgbouncer_1_22_0` (commit `e6ce619`), with
its `lib` submodule (libusual) at `46a78d2`. Tag `pgbouncer_1_22_1` (`de1e9cb`) pins the same libusual
commit and has the same `src/main.c` lines below. Read on 2026-10-11; nothing was built or run.

**The change:** a misspelled `pool_mode` in `[pgbouncer]`, e.g. `pool_mode = transction`.

**The path at startup:**
- `src/main.c:280`: `pool_mode` is a `CF_LOOKUP(pool_mode_map)` setting; `src/main.c:214` lists only
  `session`, `transaction`, `statement`.
- `lib/usual/cfparser.c` `cf_set_lookup` (line 606): compares with `strcasecmp`, returns false on
  no match (so `Transaction` would pass; a misspelling fails).
- `lib/usual/cfparser.c:167`: `parse_ini_file` logs at ERROR and stops loading:
  `invalid value "%s" for parameter %s in configuration (%s:%d)`
- `src/main.c:439` (`load_config`, called at startup from `src/main.c:985`): on the first load
  `die("cannot load config file")`, which is `log_generic(LG_FATAL, ...)` then `exit(1)`
  (`lib/usual/logging.h:170`).
- On a later load (SIGHUP reload) the same error only warns `config file loading failed` and keeps
  running (`src/main.c:441`). Only a start or restart exits.

**Exact text** for the demo's file:
- `invalid value "transction" for parameter pool_mode in configuration (/etc/pgbouncer/pgbouncer.ini:11)` (ERROR; `:12` before the runtime check)
- `cannot load config file` (FATAL)
- exit status 1, so systemd's verified line is `pgbouncer.service: Main process exited, code=exited, status=1/FAILURE`.

**Line-number quirk** (read, not run; **contradicted by the 2026-10-11 runtime check below**): this
entry said the number would be the line *after* the bad one (`:12`). The run printed `:11`. The reading
missed that `val[vlen] = 0` overwrites the value's own newline before `count_lines(buf, p)` runs, so
that newline isn't counted.

**Prefix:** `logging_prefix_cb` is set only after `load_config` (`src/main.c:989`), so these lines carry
no connection prefix. Built `--with-systemd` with stderr on the journal, the message goes through
`sd_journal_print` with no timestamp/pid/level in the text; otherwise stderr gets
`<time> [<pid>] ERROR <msg>` (`lib/usual/logging.c:227-230`). The demo logs use the bare message text
under the identifier `pgbouncer`, as the repo's other logs do.

**Upstream unit:** `etc/pgbouncer.service` at the tag is `Type=notify`. Ours is `Type=simple` (F3); F3
stands, and the "may differ" note is now confirmed.

**Still unverified:**
- Behaviour of a real 1.22 binary: now run (entry below); text and exit status confirmed, line number corrected.
- Whether our build uses `--with-systemd` (decides bare text vs. the stderr prefix in the journal).
- Whether a `logfile =` setting earlier in the file would also receive the lines (not used in the demo).
- systemd's `Failed with result 'exit-code'` and `Started …` lines: not checked, so not written.

## 2026-10-11: PgBouncer 1.22 startup failure, verified by running it

**Binary:** PgBouncer **1.22.1**, built from the release tarball
`https://www.pgbouncer.org/downloads/files/1.22.1/pgbouncer-1.22.1.tar.gz` (its published `.sha256`
checked) in a throwaway `debian:bookworm-slim` container: `./configure --prefix=/usr/local` (no
`--with-systemd`), libevent 2.1.12, OpenSSL 3.0. `pgbouncer --version` printed `PgBouncer 1.22.1`.
Run as a non-root user. The Dockerfile is in `scratchpad/pgb122/` (not committed). Container and image
removed afterwards.

**Config:** a 12-line `pgbouncer.ini` (`[databases]`, a blank line, `[pgbouncer]`, …) with line 11
`pool_mode = transction` and a non-blank line 12 (`max_prepared_statements = 200`), started as
`pgbouncer pgbouncer.ini`.

**Start with the bad file**, stderr verbatim (timestamp and pid are the run's):
```
2026-10-11 22:33:35.022 UTC [10] ERROR invalid value "transction" for parameter pool_mode in configuration (pgbouncer.ini:11)
2026-10-11 22:33:35.023 UTC [10] FATAL cannot load config file
```
Exit status **1**.

**Reload:** started with `pool_mode = transaction`, swapped in the bad file, sent SIGHUP:
```
2026-10-11 22:33:37.029 UTC [12] LOG got SIGHUP, re-reading config
2026-10-11 22:33:37.029 UTC [12] ERROR invalid value "transction" for parameter pool_mode in configuration (pgbouncer.ini:11)
2026-10-11 22:33:37.029 UTC [12] WARNING config file loading failed
```
The process kept running (checked with `kill -0` 2 s later) and exited 0 on SIGINT.

**Against the source reading:**
- Message text, ERROR then FATAL, and exit status 1: **verified**.
- Reload only warns and keeps running: **verified**.
- Line number: **contradicted**. Printed `:11` (the bad line itself), not `:12`. The demo logs and
  `demo_answer.toml` now say `:11`.
- The `%s` in the message is the path as given on the command line (`pgbouncer.ini` here). The demo
  keeps `/etc/pgbouncer/pgbouncer.ini`, which assumes our unit's `ExecStart` passes that path, as
  upstream's unit does.

**Still unverified:**
- The `--with-systemd` journal path (bare message, no timestamp/pid/level). This build had no systemd
  support, so it printed the stderr prefix. The demo keeps the bare text under identifier `pgbouncer`.
- A trailing space after the bad value. The source reading suggests the newline is counted then
  (`:12`), but this wasn't run. The demo's line has none.

## 2026-10-11: Demo incident INC-LIVE-001 (spec and logs)

**Built:**
- `eval/corpus/demo_answer.toml` filled: `likely_cause`, the mechanism (verified, entry above), the log
  chain, runbooks (RB-027, RB-011), the analog (INC-0185), related scoped config pushes (0025, 0033,
  0038, 0081), contrast bouncer incidents (0055, 0095, 0026), `key_facts`, `next_checks`,
  `forbidden_claims` and the `leak_combination`.
- `data/live/INC-LIVE-001.toml`: the demo's log spec, run through the same seeded generator
  (`rng_for("logs:INC-LIVE-001")`) into `data/processed/log_events.live.jsonl` (146 lines), hashed in
  MANIFEST (`live_log_events`). Not a document, not in `incidents.jsonl`, no LogHub noise, and
  `incident_id` is empty (the `log_events.incident_id` foreign key has no INC-LIVE-001 row, and a
  label would give the answer away).
- `synthetic_logs.live_events`: hosts, ip:port and codes checked against `services.toml` alone; the
  per-row demo guard doesn't apply, since these logs must carry the combination.
- Tests: the live logs exist and contain the combination; every host in them is in `services.toml`; an
  unknown host is rejected; refused lines come from all 4 payments-api pods and only after
  pgbouncer's exit; the spec cites only written incidents and no invented code; the existing leak tests
  still pass.

**Decisions:**
- Facts used: F1 to F5, the rollout-tool pacing for units outside the 2026-08-20 gate, and the
  PgBouncer 1.22 source check. pgx's behaviour on a failed dial is not used.
- **No `503` in the demo.** The old placeholder said the refused connections "return 503", but no fact
  says what payments-api returns when a new connection is refused (only `PAYAPI-E1042` maps to 503).
  The spec and logs say only that the connection fails.
- **INC-0153 dropped** as the near-miss incident: it is unwritten, and eval may cite only written rows.
- First refused line at 14:30:42 (was 14:30:41), so it always follows pgbouncer's exit line whatever the
  millisecond jitter.
- Logs have about as many refused connects via -01 as successful ones via -02 (30 each), matching F1's
  even split. Logs are a sample; the split is not a measured count.
- payments-pgbouncer-02 and payments-db lines are our own status wording, not PgBouncer or Postgres log
  formats (those remain unverified).

**Step 5 (ingest) must load** `log_events.live.jsonl` with the other log events, and nothing else
from the demo.

## 2026-10-11: Demo spec wording; Phase 1 step 4a (eval set, 160 drafted)

**Demo spec (Prince approved the edit):** `no_payapi_e1042` is now an observation only ("the demo logs
contain no PAYAPI-E1042 and no 53300"), and a test checks it against the live logs. The mechanism claim
about `DB_ACQUIRE_TIMEOUT` is gone. `related_incidents` no longer calls the zone-push rows Type=simple
(their coredns unit type is unstated). The forbidden claim is now "payments-api falls back to the other
bouncer (F1: no fallback)". The header cites F1-F6 and the 2026-10-11 runtime check.

**Built:**
- `eval/validate_questions.py` (+ tests). Partial mode checks every line present. `--final` also
  needs 200 lines, 40 by prince/gemini, ≥30% exact-token overall, ≥20 paraphrase among the 40, and
  the seeded split.
  `--write-split` fills the split. The split is 100/100 with `SEED`, stratified by (difficulty,
  author): one toggle across strata keeps every stratum within one of even.
- `eval/questions.jsonl`: 160 questions (author claude, split null). Three Sonnet writers split by
  cause type, then one Opus reviewer checked every label against its docs.
- `eval/label_spotcheck.md`: 40 of the 160 for Prince to check, seeded (`SEED`) and stratified by
  difficulty (exact-token 20, multi-doc 12, paraphrase 8).

**Decisions:**
- **Retelling rule (b) is one way (Prince approved).** A gold retelling write-up brings its runbook. A runbook may be
  gold alone for a procedure question. The first version required both in both directions, and the
  writers dropped runbooks that answered the question to satisfy it (W1 reported it). The reverse
  direction is the reviewer's call.
- **Twin rule, made concrete:** for each gold twin whose partner is not gold, the question must contain
  a token the gold doc has and the partner lacks: a full or short host, a service, a code, or a date
  form (ISO date, year-month, month name, month plus year, month plus day, or a bare year). A bare
  "may" doesn't count.
- 5-word runs are over the title, symptoms, root cause, resolution and body, lower-cased, using the
  word regex in the validator.
- (g) colour in key facts: any `team-*`, on-call, follow-up, reporter or owner wording, plus 0025's
  15:15/15:26/15:31/diff. (f): no `inventory-db-01` in a question when 0142 is gold.

**Review (Opus): 34 findings, all fixed.**
- 11 wrong:
  - 9 broke the short-doc rule (e) by asking a diagnosis or a count of a doc under 170 words. They
    became host, date or window lookups.
  - W1-029 had a false premise: 0083's release change is unknown.
  - W1-023 gave its answer away.
- 7 tags: six paraphrase questions are really exact-token, and W1-037 is single-doc.
- 5 missing gold: RB-008 on the journal-pattern question, RB-020, RB-011, RB-001, and INC-0179 on the
  ndots comparison.
- 3 extra gold: RB-007-only facts on 0084/0174, and RB-025/RB-024 holding no severity or duration.
- 4 fact: W1-005 planted a CPU reading 0049 doesn't record. W2-009 claimed "never rotated", which 0070
  leaves unconfirmed. W1-051 and W3-001 were near-duplicates; W3-001 also leaned on a follow-up.
- 4 voice: W3-002 handed over the numbers, W3-013 was garbled, W2-032 read backwards, and W1-031 gave
  its answer away.
- W3-018 duplicated W1-035 and was given a new angle.
- Two reviewer fixes were not taken as written. W3-002's "about 11 minutes" isn't in 0163; it became
  "within minutes of every restart". W1-005's second fact wasn't checked against the docs, so it was
  dropped.

**Human/Gemini sheet (step 7):** `eval/human_sheet.md` and `docs/gemini_prompt.md` list 40 targets.
- Targets are the least-covered incidents, 4 per cause type, taken round-robin from the least-asked
  cause types first.
- Each target shows structured fields only: service, symptom services, date, failure types, short
  hosts, codes. No titles, symptoms lines or prose.
- Mix (rebalanced): 24 paraphrase targets, marked "paraphrase: no code, no hostname"; 8 say "use
  this exact token" (specific codes first, bare HTTP numbers last); 8 are marked "multi-doc: also
  RB-NNN" (from the row's runbook_ids). The 160 already give 82 exact-token, so the human slice
  leans paraphrase. `--final` needs ≥20 paraphrase among the 40 and ≥30% exact-token overall.
- Twin, no-fix (d) and short-doc (e) targets carry their rule.
- Gemini outputs exactly 40 `N<tab>question` lines.

**Import (step 8):** `make import-human FILE=path AUTHOR=prince|gemini` appends 40 `N<tab>question`
lines as H001..H040, with empty labels, difficulty null and split null.
- It refuses anything other than exactly 40 lines numbered 1..40.
- It refuses a second import.
- Partial mode accepts unlabelled lines; `--final` rejects them.
- No split yet.

**Counts (160):** exact-token 82, multi-doc 47, paraphrase 31. Questions with 2+ gold docs: 108 (126 after the audit fixes below).
- Per cause type (the first failure type of each gold incident; runbook-only questions use the
  runbook's): api_latency 17, bad_deploy 17, conn_pool 18, disk_full 18, dns 17, high_cpu 18,
  memory_leak 14, rate_limiting 14, redis_down 13, tls_expired 14.
- Gold doc types: incident + runbook 100, incident only 57, runbook only 3.
- All 30 runbooks and all 117 written incidents are gold at least once.

**Gemini label audit (by hand, no LLM calls from code):** `make audit-pack` writes
`scratchpad/gemini_audit/corpus.md` (the 147 docs under `=== <ID> ===`, same text the validator checks;
about 447 KB) and `batch_01..08.md` (20 questions each, fixed instructions, numbered key facts).
`make import-audit FILE=path` parses Gemini's `<qid> | OK/WRONG | missing_gold | unsupported_facts |
reason` lines into `eval/label_audit.md`: every flagged question plus 10 seeded OK ones, each with a
Prince AGREE/DISAGREE line. Malformed, duplicate or unknown-id lines fail the import; missing ids are
listed. No question or label changed.
- First Gemini pass: Q081-Q160 came back rubber-stamped (all OK, generic praise, the `reason:` label
  dropped) and were discarded. The batch instructions now require the gold doc ids checked and the
  detail found for each key fact, say "If you did not open the document, answer WRONG. Generic praise is
  not a reason.", and repeat the line format at the end. The importer rejects a reason naming no
  INC-NNNN or RB-NNN id and merges several files (`FILE="a b"`). Batches 05-08 are re-run with this;
  the kept Q001-Q080 lines all cite ids.
- Audit fixes (2026-10-11): Gemini's two flags were accepted (Q091 +RB-002, Q100 +RB-001). A scan
  for gold incidents that name or link a runbook missing from the gold set found 93 (question,
  runbook) pairs. An Opus reviewer judged each against the question as worded: 20 yes (RB-008 x7,
  RB-020 x5, RB-021 x2, RB-002 x2, RB-010, RB-028, RB-009, RB-023), 73 no (narrow lookups only the
  incident holds, runbooks the incident rules out, or the wrong runbook). 22 gold additions in all;
  no question text, fact or tag changed. Q028 needed no rewording (it is about 0194). Q074 asks how
  0025's record was put back; that's allowed (0025 isn't a (d) row), but the wording half-states the
  answer, so it's left for Prince. label_spotcheck.md gold lines refreshed (same 40 questions).

**Phase 2 requirement:** Retrieval eval also reports all-gold Recall@k for questions with 2+ gold
docs, because at-least-one recall (now named Hit@k; the eval's Recall@k is the mean fraction of
gold docs found, which already gives partial credit) flatters multi-gold questions (126 of 160 after the audit fixes;
146 of the final 200).

## 2026-10-11: Phase 1 step 4 complete (eval set, 200 questions)

**Provenance**
- **160 Claude-drafted** (Q001-Q160): three Sonnet writers, then an Opus reviewer over every label
  (34 findings, all fixed; entry above).
  - Gemini label audit, pasted by hand. Q081-Q160 were audited under strict instructions (doc ids
    and details cited). Q001-Q080 were audited under loose instructions; they came back all OK and
    are not relied on.
  - Prince adjudicated the 2 Gemini flags and 10 seeded OK samples (all AGREE,
    `eval/label_audit.md`).
  - A scripted runbook-link scan across all 160, judged by an Opus reviewer: 20 runbooks added, plus
    the 2 Gemini flags.
  - Q074 was reworded so it no longer states its fix.
- **40 Gemini-drafted** (H001-H040), kept verbatim from `eval/human_sheet.md`. Labelled by a Sonnet
  labeller, then an Opus reviewer (6 findings, all fixed).
  - 14 are `false_premise`: H002, H003, H012, H018, H020, H022, H023, H024, H025, H026, H032, H033,
    H038, H040.
  - 12 have `unrecorded` parts: H004, H009, H012, H017, H018, H019, H022, H023, H026, H027, H033,
    H037.
  - H024 names the notification processor (notification-worker), so its twin INC-0128 is not gold
    (fixed after step 4; entry below).

**Validator changes in step 4b**
- Optional `false_premise` (a correcting key fact is required) and `unrecorded` (each item needs
  "not recorded: <item>"). Rule (d) passes for a no-fix row only when the fix is unrecorded.
- "name/DNS resolution" and "resolution failure" no longer count as fix wording. They had made the
  labeller add a bogus "not recorded: the fix" to H019 and H039.
- In the twin check, a service name may be written with a space ("catalog API").

**Final counts (200)**

| | exact-token | multi-doc | paraphrase | total |
|---|---|---|---|---|
| dev | 45 | 28 | 27 | 100 |
| test | 45 | 27 | 28 | 100 |
| all | 90 | 55 | 55 | 200 |

- Author: claude 160 (80 dev / 80 test), gemini 40 (20 / 20). Of the 40, 21 are paraphrase.
- Exact-token: 45% overall.
- Gold-doc count: 1 doc for 54 questions (dev 22, test 32); 2+ docs for 146 (dev 78, test 68).
- false_premise: dev 8, test 6. unrecorded: dev 8, test 4.
- The split is 100/100 with `SEED`, stratified by (difficulty, author). `--final` passes.

**Rule:** the test split is touched once, in Phase 5. In Phase 2 every eval script defaults to dev and
refuses test without an explicit flag.

**Next:** step 5, ingest and embed.

## 2026-10-11: Eval fixes after step 4 (H024 gold, re-split)

**H024 gold.** H024 asks about the "background notification processor". That names notification-worker,
which INC-0133 has and its twin INC-0128 (catalog-api) lacks, so INC-0128 was removed from the gold
set. Gold is now INC-0133, RB-005. The question text is unchanged.
- Root cause: the twin check only knew one loose spelling, a hyphen written as a space ("catalog
  API"). It didn't know "processor" for "worker", so it treated H024 as naming no service, and the
  labeller added the twin to satisfy it.
- Fix: `spellings()` in the validator. Each distinguishing token also matches with spaces for hyphens
  and with a suffix alias (`worker` → `processor`), so "notification processor",
  "notification-processor" and "notification worker" all name notification-worker. There's a test.

**Re-split.** The old split was stratified by (difficulty, author) only. That left the gold-count
buckets uneven (1 gold: dev 22 / test 32). It is now stratified by (difficulty, author, gold bucket 1
vs 2+), with the same `SEED` and the same single toggle. `--final` passes.

| | dev | test |
|---|---|---|
| exact-token | 45 | 45 |
| multi-doc | 28 | 27 |
| paraphrase | 27 | 28 |
| claude | 80 | 80 |
| gemini | 20 | 20 |
| 1 gold doc | 27 | 27 |
| 2+ gold docs | 73 | 73 |

Every (difficulty, author, bucket) stratum is within one of even. false_premise: dev 2, test 12 (all
14 are Gemini questions). unrecorded: dev 6, test 6.

**Reporting rule (Phase 2 and 5):** every eval result is reported **per author (claude, gemini) as
well as overall**. The Gemini slice holds all 14 false-premise questions, and 12 of them landed in
test, so an overall number would mix two different question styles in different proportions on each
split.

## 2026-10-11: Phase 1 step 5, ingest, chunk, embed

**Built:** `rag/ingestion.py` (`make ingest`, host-side with `HOST_DATABASE_URL`), `rag/chunking.py`,
`rag/embeddings.py`, `tests/test_ingestion.py` (throwaway `opspilot_ingest_test` DB),
`tests/test_chunking.py`. New dependencies: sentence-transformers 6.1.0 and torch 2.14.1. On Linux,
torch comes from the PyTorch CPU index (`[tool.uv.sources]`), so the Docker image gets no CUDA
wheels; the lock has no nvidia packages.

**What is loaded (only `data/processed`).** Nothing from `eval/`, NOTES, `docs/` or the demo spec is
read.
- services: 24, from services.json. Hosts, kind, port and tech have no column and are dropped.
- incidents: 117, the written rows only. Title, format, failure types, hosts, codes and symptom
  services go in `metadata`.
- log_events: 9,755. Synthetic 8,439 (with `incident_id`), LogHub 1,170 (`incident_id` NULL), live
  146 (`incident_id` NULL). If the LogHub file is missing, ingest exits and says to run
  `make corpus`.
- documents: 147 (117 incident, 30 runbook), corpus `main`. Stable ids (`INC-NNNN`, `RB-NNN`).
  Metadata: services, failure_types, hosts, codes, format, date.
  - Incidents: date is `started_at[:10]`.
  - Runbooks: date is `last_reviewed`; hosts are the services.json hostnames found in the body;
    codes come from the catalog `code_finder`.
- chunks: 408. Each has its heading path, doc type and the doc's metadata in `metadata`.

**Model:** `BAAI/bge-small-en-v1.5` at revision `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` (HF commit
"onnx support (#9)", 2024-02-22, the head of `main` on 2026-10-11). Pinned in `config.py`
(`embed_revision`). CPU, batch 64, `normalize_embeddings=True`, 384 dims, input limit 512 tokens.
The tokenizer is uncased WordPiece: `PAYAPI-E1042` is 7 tokens.

**BGE query prefix (for Phase 2 to decide):** `Represent this sentence for searching relevant
passages: `, added to queries only, never to passages.
- Source: the model card README at the pinned revision (huggingface.co/BAAI/bge-small-en-v1.5,
  model table, footnote [1] and FAQ 3).
- The card says the v1.5 models "enhance its retrieval ability without instruction" and that no
  instruction gives only "a slight degradation". It recommends picking whichever setting scores
  better on your own task.
- Phase 2 runs the retrieval ablation with and without it on dev.

**Chunking (400 / 50 tokens from config, counted with the bge tokenizer)**
- Units: headings, whole code blocks, table lines, and sentences. A sentence ends after a word, not
  after a number, so `**3. Look at…` stays one unit.
- Units are packed greedily up to 400 tokens.
- Runbooks start a new chunk at each heading; consecutive headings stay together. Incidents pack
  straight through.
- Overlap: the next chunk starts with the previous chunk's trailing sentences, up to 50 tokens.
  It is never a code block or a heading, so some boundaries get less than 50 or none.
- A heading never ends a chunk.
- A code block over 400 tokens stays whole up to 510 (bge's limit minus [CLS]/[SEP]). This happens
  twice: RB-006 (410) and INC-0080 (496).

| doc type | docs | chunks | per doc (min/avg/max) | tokens min / median / mean / max | total tokens |
|---|---|---|---|---|---|
| incident | 117 | 239 | 1 / 2.04 / 3 | 51 / 368 / 297 / 496 | 70,935 |
| runbook | 30 | 169 | 2 / 5.63 / 11 | 18 / 184 / 210 / 410 | 35,457 |

Fourteen runbook chunks are under 50 tokens. They are short reference-card intros and a few section
tails. Left as is; Phase 2 can test merging them.

**Embed time:** 408 chunks in 8.3 to 13.1 s across five full runs on the Mac's CPU, about 14 to 20 s
end to end with model load. An unchanged re-run embeds nothing and takes 0.2 s.

**Idempotency.**
- `documents.content_hash` is sha256 over title, body, metadata, chunk size and overlap, model,
  revision and the source of `rag/chunking.py`.
- Unchanged hash: the document is skipped, so its chunk rows and ids stay.
- Changed hash: the document is deleted (chunks cascade) and rebuilt.
- services and incidents are upserted. log_events is `TRUNCATE … RESTART IDENTITY` plus COPY in
  file order, so ids repeat (`ponytail:` comment: scope it to corpus `main` if injection logs ever
  arrive).
- Rows that leave the corpus are deleted. Everything runs in one transaction.
- Tests cover a second ingest leaving every row, id, content md5 and vector md5 the same, and a
  stale hash rebuilding only that document.

**Errors and fixes**
- **The third ingest re-embedded 8 runbooks with nothing changed.** Root cause: the runbook `hosts`
  metadata was sorted by length only. Equal-length hosts kept set order, which varies per process
  because Python randomises str hashes. The metadata, and so the hash, changed between runs. Fix:
  sort by name.
- **A chunker edit didn't re-chunk.** The hash covered only the data. Fix: it includes the
  chunker's source.
- **3-token chunks** ("## Investigation" alone). A heading followed directly by a sub-heading made
  its own section. Fix: consecutive headings stay in one section.
- **Stray "**3." at the end of a chunk.** The sentence splitter broke after list numbers. Fix:
  split only after a word character or closing mark.
- **Heading path `["Title"]` on first runbook chunks.** It was taken from the first unit, the H1.
  It is now taken from the first non-heading unit (`["Title", "Background"]`). Caught by
  `test_chunking`.

**Tests (16 new):** 15 in test_ingestion and test_chunking, plus the twin-alias test from the eval
fixes.
- Row counts against the corpus files.
- A second ingest changes nothing.
- A changed doc is replaced.
- No `LABEL_RE` word, sidecar label, eval question text, eval field name, `INC-LIVE-001`, demo
  question or cause, or demo leak combination in any documents or chunks row.
- Every chunk is a 384-dim unit vector.
- `opspilot_tsquery` finds PAYAPI-E1042, 503 and LEDGER-W210 (every hit holds the exact token).
  `payments-db-01` finds `payments-db-01.prod.internal`. ECONNREFUSED is in no chunk.
- Dense smoke: "ledger service JVM burning CPU because garbage collection keeps running" returns
  RB-001 first. One query, not a metric.

**Decision:** chunk content is the raw text with no title prefix. A section's later chunks may not
name their service. Prepending the doc title or heading path before embedding is a Phase 2 ablation,
not a default.

## 2026-10-11: Re-split balanced on false_premise and unrecorded

The step-4 re-split left false_premise at dev 2 / test 12. The split is now stratified by all five
dimensions together: difficulty, author, gold bucket (1 vs 2+), false_premise, and any `unrecorded`
part. Each stratum alternates dev/test. An odd stratum's extra question goes to a side drawn from
`random.Random(SEED)`, redrawn until the totals are 100/100 and every value of every dimension is
within one. It is seeded and does not depend on file order (tested). `--final` prints the table.

| | dev | test |
|---|---|---|
| exact-token / multi-doc / paraphrase | 45 / 28 / 27 | 45 / 27 / 28 |
| claude / gemini | 80 / 20 | 80 / 20 |
| 1 gold / 2+ gold | 27 / 73 | 27 / 73 |
| false_premise | 7 | 7 |
| any unrecorded | 6 | 6 |

- Error: rewriting the split test, my edit truncated the file after it and deleted
  `test_import_human_appends_40_unlabelled`; ruff then dropped its now-unused imports. Caught because
  the suite count fell from 431 to 430. Restored.
- The per-author reporting rule above still stands; false_premise is now also a reported slice.

## 2026-10-11: make gate

`make gate` runs corpus-check, test, lint, `validate_questions.py --final`, then fails if
`git status --porcelain` is non-empty. It is one `bash -euo pipefail -c` script because macOS make
is 3.81 (no `.SHELLFLAGS`). Checked: a dirty tree fails it; a lint error stops it before the validator.
Every later gate is `make gate`, with its output never piped.

## 2026-10-11: Phase 2, retrieval (ablation on dev, locked config)

**Built**
- `rag/retrieval.py`: `retrieve(query, config, conn=None)` returns ranked `Hit`s (chunk id,
  doc id, content, metadata, score). Config: mode (dense | keyword | hybrid | hybrid_rerank),
  k, candidates, bge_prefix, and filters (service, severity, doc_type, since/until on the
  doc date). A severity filter keeps incidents only.
- Migration 0002 adds `opspilot_or_tsquery`. It uses the same punctuation split as
  `opspilot_tsquery` (unchanged). Lexemes come from the simple parser. English stopwords
  (`ts_lexize('english_stem')` = `{}`) are dropped from the query only, and the rest are ORed.
  Postgres's stopword list includes "down", so "redis down" loses "down".
- `eval/retrieval_eval.py` (`make eval`):
  - 30 chunks per question, deduped to docs by best rank.
  - Recall@5, Recall@10, MRR, and all-gold R@10 on 2+ gold questions.
  - Slices by difficulty, author, gold bucket and false_premise, each with its n.
  - p50/p95 latency.
  - Every run is stored in eval_runs/eval_results with commit (with `-dirty` if the tree has
    tracked changes), config, model revisions, split and timestamp.
  - Defaults to dev. `--split test` needs `--final-test` and prints the touched-once warning.
  - Refuses to run if the DB's content hashes don't match the current chunk settings.
- `eval/ablation.py` (`run` per chunk size, `report` → `eval/results/ablation.md`, paired
  bootstrap 10,000 resamples, seed `SEED`). `eval/diagnose_ablation.py` reproduces every
  number in the findings.
- `make gate` (entry above).

**Decisions**
- Dense: `hnsw.ef_search` = max(candidates, 40) and `hnsw.iterative_scan = strict_order`,
  both `SET LOCAL`. Reason: an HNSW scan returns at most ef_search rows, so a LIMIT above it
  would come back short; iterative scan keeps going when a filter rejects rows.
  - At 408 or 547 rows the planner picks an exact seq scan.
  - Forced HNSW gives an identical top 30 on 100/100 dev questions.
- Reranker: `cross-encoder/ms-marco-MiniLM-L-6-v2` at revision
  `233902d25c440f23af6f7d6e94d2946bac0bee0a` (head of main, last modified 2026-08-09, read from
  the HF API on 2026-10-11). Pinned in `config.py`. It gets the raw query, never the bge prefix.
- Every mode is compared at the same depth: 30 chunks, then the doc dedupe.
- The claim rule is at the top of ablation.md: a difference is claimed only when its paired
  95% CI excludes 0.

**Ablation summary (dev, n = 100, Recall@5 / MRR / p95 ms)**

| config | 400/50 | 256/32 |
|---|---|---|
| dense +prefix | 0.810 / 0.734 / 12 | 0.804 / 0.746 / 15 |
| keyword | 0.668 / 0.689 / 6 | 0.690 / 0.692 / 8 |
| hybrid +prefix | 0.716 / 0.786 / 16 | 0.784 / 0.786 / 17 |
| hybrid_rerank +prefix | 0.818 / 0.910 / 565 | 0.838 / 0.885 / 639 |

Claimed, with 95% CIs:
- hybrid < dense: -0.094 [-0.158, -0.032] at 400/50.
- hybrid_rerank > hybrid: +0.102 at 400/50, +0.054 at 256/32.
- hybrid at 256/32 > 400/50: +0.068 [+0.032, +0.108].
- MRR, hybrid_rerank > dense: +0.176 overall at 400/50.

Not claimed: any prefix effect overall, and hybrid_rerank vs dense on Recall@5.

**Done-check: FAILED.** hybrid_rerank vs dense on exact-token Recall@5 is +0.022 [-0.067, +0.122]
at 400/50 and +0.044 [-0.033, +0.133] at 256/32. Why (details in ablation.md, findings 2-5):
1. `ts_rank_cd` has no IDF, and the punctuation split makes host suffixes like `01` (245/547
   chunks) dominate the rank. Keyword alone is weaker than dense even on exact-token
   questions. Cutting lexemes found in over 20% of chunks lifts keyword exact-token R@5 from
   0.744 to 0.844 (diagnostic, not adopted).
2. Dense already finds the incident, and the question's token doesn't single out the
   runbook. Dense's exact-token misses are mostly the runbook half of the gold set (13/14 at
   400/50). The runbook does contain the token: 28 of 31 gold runbooks on dev exact-token
   questions hold one of the question's codes or hostnames. But the same token is in every
   incident on that host or code, so matching it ranks the runbook among many docs that share
   it. With dense at 0.800 on n = 45, even the IDF-cut pipeline gives +0.056 [-0.022, +0.144].
   (Corrected 2026-10-11: this entry first said runbooks contain no hostnames, codes or dates.
   That was false; see "Metric definitions, Hit@k".)
3. Rerank's gain is in ordering. Gold at rank 1 on exact-token questions: dense 27-28/45,
   rerank 38-40/45. MRR CI excludes 0. That is reported as a secondary metric; it does not
   replace the failed check.

Proposed and not built (it changes the step-3 spec): IDF-weighted keyword ranking (BM25 or
ts_stat weights).

**Locked config** (`config.py`): hybrid_rerank, bge_prefix on, candidates 30, RRF k 60, chunks
256/32. Reasons and costs are in ablation.md. Re-ingested at 256/32. `make eval` on dev
(eval_runs id 16):
- Recall@5 0.838, Recall@10 0.900, MRR 0.885, all-gold R@10 0.712 (n = 73).
- Exact-token (n = 45): Recall@5 0.844, MRR 0.941.
- Gemini (n = 20): Recall@5 0.825, MRR 0.756.
- p50 420 ms, p95 597 ms.
- Dense baseline at 256/32: Recall@5 0.804, MRR 0.746, p95 15 ms.

**Sub-50-token chunks:** not false hits. They are 3-4% of chunks but take at most 1.2% of top-5
slots, with at most 3 off-gold slots per config. No merge.

**Errors and fixes**
- My split-test rewrite truncated `tests/test_validate_questions.py` (entry above).
- `diagnose_ablation` first compared 256/32 chunk results against the 400/50 runs. It read
  the chunk size from settings while the DB held 256/32. Fix: it calls `check_ingested` first,
  like the eval.
- Part A: one `pytest | tail` without pipefail. The output was visible, and every later
  gate-type command was redirected, not piped.

## 2026-10-11: Phase 2, BM25 keyword ranker (approved spec change)

- Migration 0003 adds:
  - `lexeme_df(lexeme, df)` and the one-row `bm25_stats(n_chunks, avg_len)`, rebuilt by
    `opspilot_bm25_refresh()` at the end of every ingest, in the same transaction.
  - `opspilot_query_lexemes(query)`: the OR query's split and stopword drop in one place.
    `opspilot_or_tsquery` is rebuilt from it with the same output. `opspilot_tsquery` is
    unchanged.
  - `opspilot_bm25(query)`. Candidates are `tsv @@ opspilot_or_tsquery(query)` (GIN).
    IDF = ln(1 + (N - df + 0.5) / (df + 0.5)). tf = positions in the chunk's tsvector.
    Length = total positions. k1 = 1.2, b = 0.75.
- Modes: `keyword`, `hybrid` and `hybrid_rerank` use BM25. `keyword_tsrank` keeps the old
  ts_rank_cd ranker for the ablation.
- **Duplicate lexemes.** `opspilot_tsvector` concatenates the plain and the punctuation-split
  parse, so an ordinary word gets two positions per occurrence. A hostname part gets one: the
  plain parse keeps the whole host as one `host` token.
  - tf and chunk length both count positions in that same tsvector, so length and its corpus
    average (260 positions vs 208 WordPiece tokens at 256/32) are inflated alike, and b's
    length ratio stays consistent.
  - df counts chunks, so IDF is unaffected.
  - The one asymmetry: a doubled word reaches k1's saturation sooner than a split-only host
    part. Accepted rather than re-parsing in the ranker.
- **Why k1 and b are not tuned on dev.** They are the standard defaults from the BM25
  literature. Dev has 100 questions (45 exact-token), and the ablation compares configs on
  those same questions. Tuning two free parameters on them would fit dev's noise, bias every
  dev comparison toward BM25, and make the Phase 5 test number less honest. Fixed defaults
  keep BM25 vs ts_rank a comparison of one idea, IDF plus saturation, not of a fitted model.
- First sign it works: Q003's gold chunk (INC-0015) was keyword rank 15 under ts_rank_cd and
  is rank 1 under BM25.
- Tests:
  - rare beats common at equal length; ts_rank_cd ties them.
  - `lexeme_df` equals a direct `unnest(tsv)` count, and `@@` counts for payapi, 503 and 01.
  - `bm25_stats` matches a direct count.
  - the idempotency snapshot now includes the df table and stats.
- Error: my first rare-vs-common test expected the equal-length common chunk second. A
  shorter common chunk ranked above it, which is BM25's length normalisation working. The
  test now compares only the equal-length pair.

## 2026-10-11: Phase 2 complete (BM25 rerun, final locked config, done-check)

Supersedes the locked config in "Phase 2, retrieval" above. Dev split only; test is untouched.

**Built since:** BM25 (entry above).
- Two eval diagnostics in `make eval`:
  - token-doc Recall@5: exact-token questions; the gold docs containing the question's code or
    hostname.
  - agent view: top 5 runbooks plus top 5 incidents, retrieved separately; Recall@10 and
    all-gold R@10 on the union.
- The ablation rerun at 256/32. Runs record `keyword_ranker: bm25`, so the report keeps the
  ts_rank-era runs in their own section.
- Errors:
  - The first BM25 report crashed with ZeroDivisionError. Root cause: per-question rows store
    reciprocal rank as `rr`, and the new MRR comparisons asked for `mrr` (a summary-only key),
    so no question matched. Fixed. `ci_rows` now raises a clear error when a comparison has no
    questions.
  - `latest_runs()` started returning BM25-era runs by default, which would have silently
    changed `diagnose_ablation`'s original findings. It now asks for the ts_rank-era runs
    explicitly, and they still reproduce.

**BM25 vs ts_rank_cd (256/32, n = 100), all claimed**
- Recall@5: +0.186 [+0.126, +0.249]. Exact-token +0.167 [+0.089, +0.256], multi-doc +0.181
  [+0.071, +0.301], paraphrase +0.222 [+0.093, +0.352].
- token-doc Recall@5: +0.178 [+0.089, +0.278] (n = 45). MRR: +0.201 [+0.138, +0.265].

**Headline (dev, 256/32)**

| config | R@5 | R@10 | MRR | all-gold R@10 (73) | token-doc R@5 (45) | agent R@10 | agent all-gold | p50 / p95 ms |
|---|---|---|---|---|---|---|---|---|
| dense +prefix | 0.804 | 0.910 | 0.746 | 0.753 | 0.833 | 0.928 | 0.836 | 10 / 13 |
| keyword_tsrank | 0.690 | 0.820 | 0.692 | 0.630 | 0.767 | 0.868 | 0.753 | 5 / 8 |
| keyword (BM25) | 0.876 | 0.928 | 0.893 | 0.822 | 0.944 | 0.958 | 0.890 | 27 / 60 |
| hybrid +prefix | 0.853 | 0.932 | 0.868 | 0.808 | 0.922 | 0.960 | 0.890 | 39 / 53 |
| hybrid_rerank +prefix | 0.851 | 0.915 | 0.884 | 0.767 | 0.900 | 0.959 | 0.877 | 339 / 636 |

**Locked config** (`config.py`): `keyword` (BM25, k1 1.2, b 0.75), chunks 256/32.
candidates 30 and bge_prefix on apply only to the dense and hybrid modes. Reasons, using
Prince's rule:
1. BM25 and hybrid beat dense on Recall@5 (claimed). hybrid_rerank vs dense is not claimed.
2. BM25, hybrid and hybrid_rerank are tied on Recall@5 and on MRR. No pairwise CI excludes 0.
   BM25 has the highest MRR point estimate.
3. Lowest latency: BM25 does a strict subset of hybrid's work (p50 27 vs 39 ms; rerank 339 ms).

Risk (not claimable): BM25 is weakest on paraphrase (R@5 0.815 vs dense 0.852) and on the
Gemini slice (MRR 0.705 vs hybrid 0.769, n = 20). Phase 3's agent writes its own queries. If
those paraphrase more than the eval questions do, switch to `hybrid`: it is tied on every
claimed metric, and the switch is one config value.

`make eval` on the locked config (eval_runs id 27):
- Overall: R@5 0.876, R@10 0.928, MRR 0.893, all-gold R@10 0.822 (n = 73).
- Diagnostics: token-doc R@5 0.944 (n = 45), agent R@10 0.958, agent all-gold 0.890.
- Exact-token: R@5 0.911, MRR 0.956. Paraphrase: R@5 0.815, MRR 0.732.
- claude: R@5 0.895 (n = 80); gemini: R@5 0.800 (n = 20).
- p50 26 ms, p95 55 ms.

**Done-check: failed, with cause found.** The check: hybrid_rerank beats dense on exact-token
Recall@5. Result: +0.067 [+0.000, +0.156] (n = 45); the CI does not exclude 0.
1. The runbook half of the gold set. All 12 of hybrid_rerank's exact-token misses include a
   gold runbook. The runbook usually holds the question's code or hostname (28 of 31 gold
   runbooks on dev exact-token questions), but so does every incident on that host or code.
   The shared token doesn't single the runbook out, and incidents that also match the date and
   symptoms take the top slots. The agent view, which searches runbooks separately, finds every
   gold doc for 11 of these 12 questions (BM25-rerun hybrid_rerank, eval_runs 25). (Corrected 2026-10-11: first written as "runbooks have no exact tokens", which
   was false.)
2. The ms-marco cross-encoder undoes part of BM25's token gain. On token-doc Recall@5 it is
   below BM25 alone on 5 questions and above on 1, and exact-token R@5 falls keyword 0.911 >
   hybrid 0.889 > hybrid_rerank 0.867.
3. n = 45 with dense at 0.800 puts a +0.067 gain at the edge.

The guide allows moving on once the cause is found. The check is not redefined. The
ts_rank-era failure and its cause (no IDF) are in the entry above.

**Claimable for the resume** (CI excludes 0). These are dev numbers now; only test-split numbers
from Phase 5 go on the resume.
- Locked BM25 vs dense baseline, Recall@5: 0.804 → 0.876 (+0.072 [+0.015, +0.131]).
  Exact-token +0.111 [+0.044, +0.200].
- MRR: 0.746 → 0.893 (+0.147 [+0.081, +0.212]). Exact-token +0.180 [+0.093, +0.269].
- BM25 vs ts_rank_cd keyword: Recall@5 +0.186, MRR +0.201.

**Not claimable**
- hybrid_rerank vs dense on Recall@5 (+0.046 overall, +0.067 exact-token).
- Any difference among BM25, hybrid and hybrid_rerank.
- Every prefix effect.
- Agent-view gains over dense.
- BM25 vs dense all-gold R@10 (+0.068 [-0.014, +0.164]).
- Slices with small n (gemini 20, false_premise 7) as standalone numbers.

The guide's bullet template ("hybrid RAG ... RRF fusion and cross-encoder reranking, raising
Recall@5") does not describe the locked system. It must be reworded around BM25 vs dense, or
dropped.

**Phase 3 note:** `search_runbooks` and `search_incidents` each query with their own
`doc_type` filter, which is the agent view. Separate searches lift all-gold R@10 for BM25 from
0.822 to 0.890, because a runbook no longer competes with near-twin incidents for top slots.

**Phase 5 notes**
- Re-measure latency on the EC2 instance: the locked BM25 query, and the cross-encoder rerank
  (636 ms p95 on the Mac) in case `hybrid_rerank` is ever switched on.
- Re-check `ef_search` once the corpus is large enough for the planner to pick HNSW.

## 2026-10-11: Phase 2 lock override (hybrid BM25 + dense, rerank dropped)

**Prince's decision; supersedes the `keyword` lock in the entry above.** Locked in `config.py`:
`hybrid`, which is BM25 + dense (bge prefix on) fused with RRF k = 60, no rerank, chunks 256/32,
30 candidates per leg. The code paths for the other modes stay, for the ablation.

**Reasons**
- Hybrid ties BM25 alone on every claimed metric. Recall@5 is BM25 +0.023 [-0.018, +0.064]
  and MRR +0.025 [-0.026, +0.077], neither claimed. Hybrid also beats dense on Recall@5
  (claimed, numbers below).
- Hybrid is more robust to query wording. The dense leg matches meaning when the words differ,
  and the Phase 3 agent writes its own, often paraphrased, queries. The eval questions are not
  what the agent will send.
- Directional evidence, not claimable (no CI excludes 0):
  - Paraphrase Recall@5: BM25 0.815 vs dense 0.852.
  - Gemini-slice MRR: BM25 0.705 vs hybrid 0.769 (n = 20).
- Circularity. The 160 Claude-drafted questions were written from the documents, so they
  share vocabulary with them and flatter keyword search. The 40 Gemini questions were written
  without the corpus, so that slice is the circularity check, and BM25 is weakest there.
- Rerank was built, measured and dropped:
  - no claimable gain over hybrid (Recall@5 -0.003 [-0.040, +0.035]; MRR +0.016
    [-0.033, +0.067]);
  - about 0.6 s p95 on the Mac (636 ms) against 53 ms for hybrid;
  - it demotes rare-token docs: below BM25 alone on 5 exact-token questions' token-doc Recall@5
    and above on 1.
  - The `hybrid_rerank` mode stays in the code.

**Hybrid vs dense (dev, 256/32, paired bootstrap, 10,000 resamples, seed `SEED`)**

| metric | slice | n | hybrid - dense | 95% CI | claim |
|---|---|---|---|---|---|
| Recall@5 | overall | 100 | +0.049 | [+0.006, +0.095] | yes |
| MRR | overall | 100 | +0.122 | [+0.073, +0.170] | yes |
| Recall@5 | exact-token | 45 | +0.089 | [+0.033, +0.156] | yes |
| token-doc Recall@5 | exact-token | 45 | +0.089 | [+0.033, +0.156] | yes |
| Recall@5 | paraphrase | 27 | -0.019 | [-0.111, +0.074] | no |

**`make eval` on the locked config** (eval_runs id 29) reproduces the ablation's
`hybrid +prefix @256/32` row exactly.
- Overall: R@5 0.853, R@10 0.932, MRR 0.868, all-gold R@10 0.808 (n = 73).
- Diagnostics: token-doc R@5 0.922 (n = 45), agent R@10 0.960, agent all-gold 0.890.
- Exact-token: R@5 0.889, MRR 0.902. Paraphrase: R@5 0.833, MRR 0.783.
- claude: R@5 0.860 (n = 80); gemini: R@5 0.825, MRR 0.769 (n = 20).
- Latency: p50 40 ms, p95 86 ms. The ablation run measured 39 / 53 ms; retrieval metrics are
  deterministic, latency is not.

**Done-check:** unchanged. It is about hybrid_rerank vs dense on exact-token Recall@5: failed,
with cause found (entry above), and it is not redefined.

The locked config (hybrid, no rerank) beats dense on exact-token Recall@5: +0.089 [+0.033, +0.156], claimed. The original check (hybrid+rerank) stays recorded as failed with cause.

**Claimable numbers** (these replace the list in the entry above; dev now, test in Phase 5)
- Locked hybrid vs the dense baseline: Recall@5 0.804 → 0.853 (+0.049 [+0.006, +0.095]).
- MRR 0.746 → 0.868 (+0.122 [+0.073, +0.170]).
- Exact-token Recall@5 0.800 → 0.889 (+0.089 [+0.033, +0.156]).
- Token-doc Recall@5 0.833 → 0.922 (+0.089 [+0.033, +0.156]). Not independent of the line
  above: on 42 of 45 exact-token questions the token-gold set is the whole gold set, and the
  same 7 questions changed in both. Count it once.
- BM25 vs ts_rank_cd keyword (Recall@5 +0.186, MRR +0.201) remains a claimable engineering
  result.

**Not claimable**
- Hybrid vs dense on paraphrase, all-gold R@10, or the agent view.
- Any hybrid vs BM25 or hybrid vs hybrid_rerank difference.
- Every prefix effect.
- Small slices as standalone numbers.

**Resume bullet:** "hybrid BM25 + dense vs dense baseline". Its numbers come from the
test split in Phase 5 only (`make eval ARGS="--split test --final-test"`, once), never from
these dev numbers. Rerank and RRF wording stays out unless test supports it: RRF is in the
system, rerank is not.

**Phase 5 note:** re-measure the locked hybrid's latency on EC2. Re-measure the rerank latency
only if `hybrid_rerank` is ever switched back on.

## 2026-10-11: Metric definitions, Hit@k, done-check note

**Recall@k definition.** `eval/retrieval_eval.py` computes, per question, |gold ∩ top k| / |gold|,
averaged over questions. That is the mean fraction of gold docs found. It is not the build
guide's old "share of questions with at least one gold doc in the top k". The metric is kept and
named "Recall@k (mean fraction of gold docs found)" everywhere: the eval table, ablation.md and
BUILD_GUIDE's metrics table.
- **Hit@5 and Hit@10** (at least one gold doc in the top k) were added. Stored runs get Hit@k
  from their saved top-10 doc ranking (`with_hits`), which is exact, so no run was redone.
- Hybrid vs dense on dev, at 256/32 (none claimed; each CI's lower bound is 0):
  - Hit@5: 0.930 → 0.970, +0.040 [+0.000, +0.090].
  - Hit@5, exact-token: +0.067 [+0.000, +0.156] (n = 45).
  - Hit@10: 0.980 → 0.990, +0.010 [+0.000, +0.030].
- Hit@k is near its ceiling (0.93 to 0.99), which is why the at-least-one version flatters
  multi-gold questions: finding the incident alone counts as a full hit. Recall@k and all-gold
  Recall@10 are what separate the configs.

**Exact-token Recall@5 and token-doc Recall@5 give identical differences and CIs.** This is not
a bug.
- For 42 of 45 dev exact-token questions, every gold doc contains one of the question's codes
  or hostnames, so the token-gold set is the whole gold set.
- On the other 3, neither metric changed between dense and hybrid.
- The questions that changed are the same in both, with the same values: Q001 0.5→1, Q013
  0→0.5, Q038 0→0.5, Q044 0.5→1, Q089 0.5→1, Q127 0.5→1, Q140 0→1. Same per-question
  differences and the same seed give the same bootstrap CI.
- On this eval set, token-doc Recall@5 is not independent evidence. ablation.md now prints the
  42/45 count, computed at report time.

**Correction.** Earlier entries said gold runbooks contain no hostnames, codes or dates. That is
false: 28 of 31 gold runbooks on dev exact-token questions contain one of the question's tokens
(found while checking the identical CIs). The token is shared with every incident on the same
host or code, so it doesn't single the runbook out among many matching docs. The agent view's
gain (runbooks searched separately) fits this. The wrong sentences in NOTES and ablation.md are
marked "Corrected" where they stand.

**Done-check note** (also under the Phase 2 lock override): the locked config (hybrid, no rerank)
beats dense on exact-token Recall@5: +0.089 [+0.033, +0.156], claimed. The original check
(hybrid+rerank) stays recorded as failed with cause.

## 2026-10-11: Results ledger (docs/results.md)

- `make results` runs `scripts/results.py`, which writes `docs/results.md` from eval_runs, the
  ingested DB and repo files. No number in it is typed by hand. Sections:
  - (a) corpus and eval-set facts, with label-audit counts parsed from `eval/label_audit.md`;
  - (b) retrieval levels and locked-vs-dense comparisons, each with definition, split, n,
    value, CI, claimable, eval_run id and commit;
  - (c) "not measured yet (Phase N)" placeholders;
  - (d) BUILD_GUIDE's bullet templates, each mapped to the rows that fill it.
- `make results-check` is in `make gate`. It regenerates the ledger in memory and fails when the
  committed file differs, e.g. after any new `make eval` run, since the ledger cites the newest
  run. The output is deterministic: no wall-clock time, and the bootstrap uses seed `SEED`.
- Rows use the newest locked-config run and the newest dense-baseline run per split. Both
  current dev runs (32, 33) were made from a clean tree at 38c2539, so their commits carry no
  `-dirty`.
- Claimable: "n/a" for levels; for comparisons, yes only when the CI excludes 0. Resume
  numbers come from test rows only. Those rows appear after the Phase 5 run and say "not
  measured yet (Phase 5)" until then.
- BUILD_GUIDE's first bullet template now reads "hybrid BM25 + dense ... from [X]% for a dense
  baseline to [Y]%". Rerank is no longer in it.
- The agent-view R@10 comparison for locked hybrid vs dense is +0.032 [+0.002, +0.069]. The
  CI excludes 0, but it is a diagnostic, not a resume metric.
- Error: the first render put raw `|` characters (from |gold ∩ top k|) inside table cells, which
  breaks markdown tables. The definitions are reworded without pipes.

## 2026-10-12: Results ledger reads committed files

- `make eval` writes `eval/results/<split>_locked.json` and `<split>_dense_baseline.json` for
  the two named configs (`named()`). Each holds per-question scores (Recall@k, Hit@k, rr,
  all-gold, token-doc, agent view, top-10 docs), metrics by slice, config, model revisions, a
  corpus snapshot (documents, chunks and log lines as ingested), the commit, the tracked files
  that differed from it, and p50/p95 latency. There are no timestamps or run ids.
- If a run's deterministic part (scores, metrics, config, models, corpus) matches the file,
  the file is not rewritten. Its commit and latency stay those of the run that first produced
  the numbers. Latency varies run to run, so it is reported "from the run that produced these
  scores".
- `scripts/results.py` reads only eval/results/*.json, eval/questions.jsonl, data/processed and
  eval/label_audit.md. It never imports or opens the database. The chunk and log counts come
  from the result file's snapshot, because a clean clone has neither a DB nor the LogHub
  download.
- `make results-check` (in `make gate`) fails only when the regenerated ledger differs. That
  now happens only when a metric, count or definition changes.
- Checks:
  - `make eval` (locked, twice) and `make eval ARGS="--mode dense"`: files "unchanged" and
    byte-identical while latency moved (p95 100 then 104 ms); `results-check` passes.
  - Tests: an identical rewrite is skipped and a changed score is written; the ledger renders
    with `psycopg.connect` patched to fail; the result files hold no run ids or timestamps.
  - A fresh clone with no reachable database rebuilds docs/results.md byte for byte (run after
    this commit, below).
- The current files cite commit 0618241 plus 2 uncommitted files (eval/retrieval_eval.py,
  scripts/results.py). Those are the result-writing code. The retrieval code and config are
  unchanged since 0618241, and the numbers equal eval_runs 32/33 made from a clean tree.
- The Phase 2 done-check cause text now reads coherently after the runbook-token correction.
  The token is in the runbook but shared with every doc on the same host or code; it is not
  missing.

## 2026-10-12: Phase 3a, the agent (BUILD_GUIDE Phase 3 steps 1-4)

**Built**
- Results provenance: identical scores rerun from a clean tree replace a dirty provenance
  (commit and uncommitted_changes only; latency stays the first run's). Result outputs
  (eval/results, docs/results.md) no longer count as uncommitted code. Both dev files now cite
  1735a76 with no uncommitted files; docs/results.md differs from before only in the commit cells.
- `llm/client.py`, the one LLM entry point. Plain httpx, no provider SDKs: Groq and Gemini
  through their OpenAI-compatible chat endpoints, Ollama through native `/api/chat` (only it
  takes `options.num_ctx`, sent as 8,192 in every request). Temperature 0, tool calling,
  JSON-schema output, streaming. Retries on 429/5xx/transport errors: retry-after when given,
  else exponential backoff with jitter, capped at 30 s and 5 attempts; a retry-after over 60 s
  (a daily limit) raises at once. Disk cache (`.cache/llm`, gitignored) keyed by
  sha256(provider, model, messages, tools, params). Per-call budget 6,000 tokens (estimated
  prompt + max output), raised as `BudgetExceeded` before any request. Token counts from usage
  fields, flagged `estimated` when missing. Langfuse generation per call, trace id =
  investigation id; the SDK is never constructed unless TRACING_ENABLED and both keys are set.
  Models are config (`AGENT_LLM`, `JUDGE_LLM`, `DEV_LLM` as provider:model). A test fails if
  any file outside llm/ imports a provider SDK (or langfuse) or names a provider endpoint.
- `agent/tools`: search_runbooks / search_incidents (locked hybrid retrieval with a doc_type
  filter, deduped to docs, top 5, ids `DOC#chunk`, snippets 400 chars), query_logs (one service,
  window <= 24 h, minimum level, literal text match, <= 50 rows by time, ids `log:<id>`, a note
  saying "50 of N"), get_service_status (`status:<name>`). Migration 0004 adds role
  `opspilot_ro`: SELECT only, `default_transaction_read_only = on`. Tests: INSERT, UPDATE and
  DELETE fail through the tools' connection, and still fail with the read-only transaction
  switched off (InsufficientPrivilege). The role is cluster-wide, so downgrade revokes grants
  but does not drop it.
- `agent/graph.py`: triage (LLM JSON) -> plan (LLM tool calls) -> gather -> draft (LLM JSON,
  one retry on invalid output) -> verify (deterministic) -> gather (<= 2 loops) | finish.
  Reducers: tool_calls and llm_calls append, evidence merges by id. The draft sees status, then
  logs with near-identical lines collapsed (numbers and hex ids masked; the other ids stay
  citable), then runbook and incident snippets (300 chars), packed until the token allowance is
  spent. Caps: 8 tool calls, 300 s. After the loop cap: invalid-citation claims dropped,
  confidence down one level, `citations_verified: false` plus a note. VERIFY_ENABLED=false
  routes draft to finish (`citations_verified: null`).
- API: `POST /investigate` streams SSE (tool_call, tool_result, sources, diagnosis, error),
  id in `X-Investigation-Id`; `GET /investigations/{id}`. Migration 0005: investigations
  (question, as_of, llm, verify flag, status, diagnosis, llm_calls with per-call tokens,
  totals) and the tool_calls FK plus `output_tokens` (estimate). Tool calls are written as each
  one finishes.
- `make demo ARGS="--llm ... --out ..."` writes a transcript (scripts/demo.py).

**Tests** (stub LLM = the real Client with only the HTTP post replaced, so budget checks and
token logging run for real): verify loop fires on a bad citation and stops at 2 (3 drafts, 2
follow-ups, claim dropped, confidence lowered); a redraft with gathered ids passes; uncited
claims fail verify; the flag disables verify; the tool-call cap holds (12 planned, 8 run, no
follow-up once capped); no call exceeds 6,000 on an overfull gather and the draft drops
evidence; invalid JSON gets one retry; SSE order; tool_calls persisted with the FK; a
tracing-disabled run never constructs Langfuse; num_ctx is sent; retry-after honoured.
526 tests pass.

**Errors and fixes**
- Provenance: the dense rerun stayed dirty because the locked rerun had just rewritten
  dev_locked.json, a tracked file. Root cause: result outputs counted as uncommitted code.
  Excluded with a git pathspec; test added.
- `make demo` with `TRACING_ENABLED=false` in the shell failed pydantic's bool parse
  (`'true         '`). Root cause: `-include .env` makes a make variable from the .env line
  (inline comment stripped, trailing spaces kept), and make re-exports an environment variable
  with the makefile's value. Pass overrides as make arguments: `make demo TRACING_ENABLED=false`.
- `host.docker.internal` does not resolve on the Mac; host-side `make demo` sets
  `OLLAMA_HOST=http://localhost:11434`.
- Langfuse: `GET /api/public/traces/{id}` returns `LEGACY_API_UNAVAILABLE_FOR_NEW_ORGANIZATION`
  for orgs created after 2026-09-16; use `GET /api/public/v2/observations?traceId=...` with
  `fields=core,basic,usage,model`. The legacy ingestion API is sunset 2026-11-16, so tracing
  uses the SDK (OpenTelemetry).

**Token estimate vs provider counts.** The estimate (chars / 3 over messages, tools and schema)
was above the provider count on every real-size prompt (qwen: 525/220, 968/715, 2466/1888;
Groq: 525/488, 969/631, 1070/902). It is below on tiny prompts because of fixed chat-template
overhead (Groq counted 78 tokens for a 22-token estimate of "pong").

**Demo smoke** (one run each, not a metric; transcripts in scratchpad/phase3/, judged by
Claude against eval/corpus/demo_answer.toml)

| | Ollama qwen2.5:7b | Groq gpt-oss-120b |
|---|---|---|
| tool calls | 8 (cap) | 1 |
| key facts hit | 0 of 9 | 0 of 9 |
| forbidden claims | 1 (payments-db max_connections) | 1 (payments-db disk full) |
| cited ids gathered (deterministic) | 2 of 2 | 2 of 2 |
| claims supported by their citation (judged) | 0 of 2 | 0 of 2 |
| verify loops | 0 | 0 |
| tokens prompt / completion | 2,823 / 860 | 2,021 / 1,068 |
| wall time | 107.7 s | 10.6 s |

- qwen planned 8 calls but spent 5 on ERROR logs of payments-db, card-processor-proxy, auth-db
  and auth-service, never payments-pgbouncer (listed degraded by get_service_status).
- gpt-oss returned one tool call per turn, as a tool-calling loop expects. The graph gives the
  model one planning turn, so it saw only 5 runbook snippets and diagnosed from a runbook's
  description of a disk-full incident, with confidence high.
- Verify passed both runs: every cited id was gathered. Neither citation supported its claim.
  That is the deterministic/judged gap that citation accuracy (a) vs (b) measures.
- **Open design question for Prince:** gather is single-shot. A multi-turn gather (the model
  sees results and picks the next call until it stops or hits the cap) is the standard fix,
  and costs more tokens per run on Groq's 8K TPM. Not changed in 3a.

**Provider smoke (2026-10-12)**: .env has Groq, Gemini and Langfuse values (present; values
never printed).
- Groq `openai/gpt-oss-120b`: text call OK; tool call OK (`get_service_status`
  {"service": "payments-api"}). Tool use is listed on Groq's model page. No switch to qwen3.8.
- Gemini `gemini-3.5-flash-lite` (id from ai.google.dev/gemini-api/docs/models): text call OK
  through the OpenAI-compatible endpoint.
- Langfuse: one investigation traced with TRACING_ENABLED=true; `v2/observations` returned its
  3 generations under trace f2957c85345d4715b49fb21475c7e1e6 with model and token usage equal
  to the DB rows. Region: US (us.cloud.langfuse.com). Closes Phase 1's Langfuse check.

**Free-tier limits** (observed by Prince in the consoles, 2026-10-12)
- Groq `openai/gpt-oss-120b` and `qwen/qwen3.8-27b`: 30 RPM, 1K RPD, 8K TPM, 200K TPD.
  Groq's rate-limits page lists the same row for gpt-oss-120b as the base limits; limits are
  per organization, 429s carry retry-after (seconds), cached tokens don't count.
- Gemini Flash models: 5 RPM, 20 RPD. Gemini 3.5 / 3.1 Flash Lite: 15 RPM, 500 RPD, 250K TPM.
- Groq also offers `llama-prompt-guard-2` (Phase 4 defense candidate).
- Langfuse Hobby: 50k units per month (a unit is any trace, observation or score), 30 days of
  data, 2 users (langfuse.com/pricing). One investigation is 1 trace + 3 to 9 generations, so
  about 4 to 10 units.

**Daily capacity on Groq's free tier (agent only, 200K TPD binds before 1K RPD)**
- Measured run (3,089 tokens): about 64 per day. That run made 1 tool call, so its draft was
  small; treat 64 as optimistic.
- Ceilings from the 6,000-token per-call budget: 3 calls without loops <= 18K tokens, about
  11 per day; 7 calls with 2 loops <= 42K, about 4 per day.
- 8K TPM: a run above 8K tokens within a minute waits on 429 retry-after; the client sleeps up
  to 60 s per retry.
- A 100-question dev run does not fit one day's free quota unless runs stay near the measured
  size; plan for the response cache and several days, or the paid tier.

**Cost estimate, 100-question dev run on Groq's paid tier** (gpt-oss-120b $0.15 per M input,
$0.60 per M output, Groq's model page console.groq.com/docs/model/openai/gpt-oss-120b; the
groq.com/pricing page did not render prices to a fetch)
- At the measured run: 100 x (2,021 x 0.15 + 1,068 x 0.60) / 1M = $0.09.
- Ceiling (42K tokens per run, all priced as output): $2.52. Judge calls (Gemini) excluded.

**Deferred**: generation eval, judge, hand labels (Phase 3b); injection defenses (Phase 4);
the gather design question above.

## 2026-10-12: Phase 3a.5, an agent that investigates; Groq vs Gemini as the agent

**Built**
- Iterative gather (Prince's decision, closes 3a's open design question): the plan node is gone;
  gather is a loop where the model calls a tool, sees the result and picks the next call, until
  it replies without a tool call (done) or the run reaches 8 tool calls. State keeps every item;
  each turn's prompt holds the calls so far, a compact index of older evidence (id + 100 chars,
  near-identical log lines collapsed) and only the latest turn's results in full (capped at
  1,500 tokens), packed to the 6,000-token budget. Measured: the largest gather prompt
  estimate in the 10 final dev runs was 2,843 tokens (+ 1,000 max output). The client now records seconds slept on 429/5xx per call
  (`wait_s`).
- Quote-grounded verify: each claim carries a quote; verify checks, with no LLM, that the
  normalized quote (casefold, whitespace collapsed, curly quotes straightened, surrounding quote
  marks and ellipses stripped) is a substring of one cited item and has at least 3 words. A
  failed quote counts like a bad id: back to gather (<= 2 loops), then the claim is dropped and
  confidence goes down. `VERIFY_QUOTES` (default true) next to `VERIFY_ENABLED`. Per-pass counts
  (claims, bad ids, bad quotes) go out with the diagnosis event as `verify_log`.
- Historical windows: the system prompt states as_of and says to look at a past date when the
  question names one; gather and triage ask for an explicit window around a named date (whole
  day if no time is given). `query_logs` already took any window of <= 24 h; only the prompts
  pinned it to as_of. Test: a stub that sets a January 2026 window gets exactly INC-9001's logs
  through the real tool.
- `make demo ARGS="--qid Qxxx"` runs a dev question (test ids refused) and writes a JSON summary
  beside the transcript; `--pick N --seed S` is the stratified sample.

**Errors and fixes (root cause first)**
- Groq runs all failed on triage with 400 `json_validate_failed`, empty `failed_generation`.
  Root cause: gpt-oss-120b reasons before answering and the reasoning counts against
  max_tokens; the longer step-3 triage prompt made it think past 400 tokens (probe on Q063: 400
  fails, 1,000 passes with 465 completion tokens). TRIAGE_MAX_TOKENS 400 -> 1,000.
- Groq drafts hit max_tokens exactly (1,500 completion tokens) and came back truncated: H007
  fell back to "no valid diagnosis", and on Q074 Groq raised the truncation as a 400
  `json_validate_failed`, which crashed the run. DRAFT_MAX_TOKENS 1,500 -> 2,500 (final runs
  used up to 2,365); a `json_validate_failed` 400 now gets the same one retry as invalid JSON
  (its tokens are unrecorded: Groq returns no usage with the error). None occurred in the final
  runs.
- Gemini runs died on 429 after about 10 s. Root cause: Flash Lite's free tier allows 15
  requests per minute; Gemini sends no retry-after header but puts the delay in the body
  ("Please retry in 50.8s", RetryInfo `retryDelay`), so the client fell back to short
  exponential backoff and gave up after 5 attempts. The client now reads either form from the
  body.
- Both models repeated calls (Gemini searched "INC-0109" three times; Groq repeated a runbook
  search). Root cause, from the dev transcripts: search tools return the best chunk per document,
  so repeating a query or searching by a document id can never show another part of that
  document; INC-0109's key facts sit in chunk 1, the queries matched chunk 0. Fixed below.

**Prompt and graph changes from dev questions (never the demo); a and c re-run after them**
1. GATHER_INSTRUCTIONS: "Searches return the best-matching part of each document, one part per
   document: to read another part of a document you found (its cause, fix or timeline), search
   with words that part would contain, not with the document's id." Reason: Q063, Q074, H007,
   Q053 (both models) re-searched an incident by id to read more of it.
2. A repeated call (same tool, same arguments) is not run; it counts toward the cap and comes
   back as a failed call naming the original. Reason: same transcripts; prompt text alone
   ("Do not repeat a call") was ignored. Tool-call success below counts these as failures.
3. TRIAGE_MAX_TOKENS and DRAFT_MAX_TOKENS raised (above).
First-round transcripts (before 1-3) are in scratchpad/phase3/a5/v1/; the table uses the final
code for every row.

**Smoke runs** (one run per cell, not metrics; seed 0 stratified dev sample: Q063, Q074
exact-token; H007, Q017 multi-doc; Q053 paraphrase; as_of 2026-10-06T14:52:00Z; transcripts in
scratchpad/phase3/a5/). Key facts and contradictions judged by Claude, strict: a fact counts
only if the diagnosis states it (partials in brackets). Dev questions list no forbidden claims,
so the dev column counts claims that contradict the gold.

| run | model | tool calls ok | key facts | forbidden / contradicting | id fails per pass | quote fails per pass | loops | LLM calls | tokens prompt / completion | max call (actual) | wall s | 429 wait s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Q063 | Groq | 6/8 | 0/3 (1) | 0 | 0 | 0 | 0 | 10 | 13,120 / 2,258 | 2,377 | 36.4 | 17.0 |
| Q063 | Gemini | 8/8 | 0/3 | 0 | 0 | 0 | 0 | 10 | 17,758 / 600 | 2,342 | 19.4 | 0.0 |
| Q074 | Groq | 8/8 | 0/2 | 0 | 0,0 | 1,0 | 1 | 11 | 17,120 / 4,406 | 3,708 | 154.3 | 100.0 |
| Q074 | Gemini | 6/6 | 0/2 | 1 (cache turnover as the cause) | 0 | 0 | 0 | 9 | 16,400 / 657 | 2,683 | 16.0 | 0.0 |
| H007 | Groq | 7/8 | 0/3 (1) | 1 (pool saturated; pool-size rollback) | 0,0 | 1,0 | 1 | 11 | 16,666 / 5,797 | 4,449 | 134.6 | 107.0 |
| H007 | Gemini | 8/8 | 0/3 (1) | 0 | 0,0 | 1,0 | 1 | 10 | 21,014 / 964 | 3,179 | 25.6 | 0.0 |
| Q017 | Groq | 4/4 | 1/2 | 0 | 0 | 0 | 0 | 7 | 9,194 / 3,314 | 2,809 | 53.6 | 38.0 |
| Q017 | Gemini | 8/8 | 0/2 | 1 (not the same pod) | 0,0 | 1,0 | 1 | 11 | 19,354 / 1,191 | 2,364 | 53.6 | 33.7 |
| Q053 | Groq | 8/8 | 0/2 (1) | 0 | 0,0 | 1,0 | 1 | 12 | 21,756 / 4,980 | 3,910 | 164.9 | 138.0 |
| Q053 | Gemini | 4/4 | 2/2 | 0 | 0 | 0 | 0 | 6 | 9,388 / 829 | 2,271 | 16.4 | 0.0 |
| demo | Groq | 7/8 | 1/9 | 0 | 0,0,0 | 1,1,1 | 2 | 12 | 21,366 / 5,165 | 4,040 | 181.3 | 157.0 |
| demo | Gemini | 7/7 | 2/9 | 0 | 0 | 0 | 0 | 8 | 13,297 / 1,342 | 2,580 | 18.3 | 0.0 |
| demo | Ollama qwen2.5:7b | 6/8 | 0/9 | 1 (pool exhaustion, PAYAPI-E1042) | 0,0,0 | 3,1,0 | 2 | 12 | 22,964 / 1,382 | 2,728 | 279.1 | 0.0 |

Totals, 5 dev questions: Groq 1/12 key facts (3 partial), 1 contradiction, 3 loops, 98,611
tokens (mean 19.7K per run), 51 requests; Gemini 2/12 (1 partial), 2 contradictions, 2 loops,
88,155 tokens (mean 17.6K), 46 requests. Every call stayed within budget (estimate + max_tokens
<= 6,000 is enforced before sending); "max call (actual)" is the provider's prompt +
completion count, reasoning included.

- Gemini found the demo mechanism (pgbouncer rejected pool_mode "transction" and exited) and
  the refused 10.0.4.21:6432 connects; Groq stopped at "payments-pgbouncer degraded" and listed
  disk and pool exhaustion as next checks (checks, not claims). qwen claimed pool exhaustion
  after its own PAYAPI-E1042 log queries returned 0 rows.
- The quote check flagged first drafts in 7 runs (4 Groq, 2 Gemini, 1 qwen), and missed two ways:
  (1) qwen's PAYAPI-E1042 claim and the first-round qwen disk-full claim both passed with real
  quotes from runbooks describing other incidents: a quote proves the words exist, not that they
  support this claim; that is citation accuracy (b), the judge's job. (2) Groq's demo claim
  "payments-api: degraded" was dropped: a real 2-word quote under the 3-word floor.
- Both models score low on dev for the same, model-independent reason: they reach the right
  incident (the gold incident was in the gathered evidence in 10 of 10 dev runs, every gold doc
  in 5 of 10) but see only one chunk of it.
  Open question for Prince: a read-more tool (next chunks of a document) is the likely fix; it
  would be a fifth tool, so not added.
- qwen queried `payments-pgbouncer-01` (a host) as a service in the first round and got
  "0 of 0 rows" with no hint; query_logs could name the known services as get_service_status
  does. Not changed.

**Capacity and cost** (from the runs above; limits as recorded in Phase 3a)
- Groq free tier, 200K tokens/day: mean 20.9K tokens per run over all 6 Groq runs (range
  12.5K to 26.7K): about 9 runs/day (7 to 16). 1K requests/day allows about 95 (10.5 requests
  per run), so tokens bind. 8K TPM costs 17 to 157 s of 429 waits per run. Recorded Groq use
  today (3a, first round 54K, triage probe, final round 125K) is about 185K tokens, plus one
  truncated draft Groq reported no usage for: near the daily cap.
- Groq paid (console.groq.com/docs/model/openai/gpt-oss-120b, checked 2026-10-12: $0.15 per M
  input, $0.075 per M cached input, $0.60 per M output): dev means 15,571 prompt + 4,151
  completion tokens per run -> $0.0048 per run, about $0.48 per 100-question dev run (cache
  discount ignored; judge excluded). Ceiling at the largest run (26.7K, all priced as output):
  $1.60.
- Gemini free tier, 500 requests/day, 15 RPM: 9.2 requests per dev run -> about 920 requests
  per 100-question run, 1.8 days (2 days); at 15 RPM at least about 61 minutes of runtime.
  Judge calls not counted (the judge does not exist yet).

**Recommendation** (Prince decides; config unchanged): Gemini Flash Lite's quality is close to
Groq's on these 6 questions (dev 2/12 vs 1/12 key facts, 2 vs 1 contradictions; demo 2/9 vs
1/9, both no forbidden claims). One run per cell cannot separate them, so "close" is the
honest reading. Recommend Gemini Flash Lite as the agent (free); the judge then moves to
groq / openai/gpt-oss-120b, a different family, and the judge's 200K tokens/day sets the pace
of a full eval. Gemini was also 1 to 10 times faster per run here, mostly because Groq's 8K
TPM forced waits.

**Tests**: 545 pass (loop stops on done, at the cap, never exceeds the budget; a real quote
passes, an invented one fails and loops, flag off skips it, normalization cases; past-incident
window; repeated call not run; json_validate_failed retried; Gemini body retry delay; sample is
stratified and seeded).

## 2026-10-12: Phase 3a.6, OpenRouter provider, read_document, agent and judge config

**Decision (Prince):** agent = `openrouter:openai/gpt-oss-120b`, pinned to Groq; judge =
`gemini:gemini-3.5-flash-lite` (free); dev = `ollama:qwen2.5:7b` (unchanged).
- Why not Groq direct: Groq's paid upgrade was unavailable, and its free tier (200K tokens/day,
  8K TPM; Phase 3a entry) allows about 9 agent runs a day, far short of a 100-question eval.
- OpenRouter is a router in front of several hosts of the same model. Every request sends
  `provider: {order: ["groq"], allow_fallbacks: false, require_parameters: true}`
  (openrouter.ai/docs/features/provider-routing): one upstream host, fail instead of rerouting,
  and only a host that supports every parameter sent (tools, response_format). Checked live: the
  reply's `provider` field was `Groq`. Slug `groq` from OpenRouter's endpoints API
  (`/api/v1/models/openai/gpt-oss-120b/endpoints`).
- Money: $5 prepaid OpenRouter credit, a hard cap (an empty balance returns 402, which the client
  does not retry).
- Agent and judge are different model families (OpenAI gpt-oss vs Google Gemini), so the judge
  is not grading its own family's output.

**Prices** (checked 2026-10-12)
- gpt-oss-120b via OpenRouter, Groq provider: $0.15 per 1M input, $0.60 per 1M output, $0.075
  per 1M cached input (openrouter.ai/openai/gpt-oss-120b and the endpoints API; same as
  groq.com/pricing). OpenRouter adds no markup on these.
- Gemini 3.5 Flash-Lite: free tier (500 requests/day, 15 RPM), no cost.

**Cost per call:** OpenRouter returns the charge in `usage.cost` on every response by default
(openrouter.ai/docs/use-cases/usage-accounting). The client records it as `cost_usd`
(`cost_source` "reported"); without it, tokens times PRICES_PER_M ("computed"); free providers
"free". Live check: 77 prompt + 39 completion tokens, reported $0.00003495 = 77 x 0.15/1M + 39 x
0.60/1M exactly. A cache hit keeps the original cost but is marked cached, so the eval counts it
as no new spend.

**Per-run estimate (before measuring):** 3a.5 Groq dev means, 15,571 prompt + 4,151
completion tokens: $0.0023 + $0.0025 = about $0.005 per run, about $0.50 per 100-question dev
run, about $1.00 for verify on plus off. Ceiling at the largest 3a.5 run (26.7K tokens, all
priced as output): $0.016 per run, $3.20 for both configs. read_document will add prompt tokens;
measured in the confirm runs below.

**read_document (fifth tool):** `read_document(doc_id, part=1)` returns a document's chunks in
order, full text, ids `DOC#index` as search gives them, paged at PAGE_TOKENS 1,400 estimated
tokens (one page plus header fits gather's 1,500-token latest-result slot). On the corpus: 115 of
117 incidents are one page, 2 are two; runbooks 16 one, 13 two, 1 three; the largest page is
1,392. A missing id or part is a failed call (ToolError). Gather's prompt now says: search to
find documents, then read_document for details. The draft sees read chunks whole (READ_CHARS
2,000) after status and logs, before the 300-char search snippets; a full read replaces a
search snippet with the same id in the evidence map, so quotes from the rest of the chunk verify.

**Confirm runs (step 4)**: same 5 dev questions (Q063, Q074 exact-token; H007, Q017 multi-doc;
Q053 paraphrase) plus the demo, as_of 2026-10-06T14:52:00Z, with read_document, on gpt-oss-120b
(OpenRouter, Groq upstream) and on Gemini Flash-Lite as the agent. One run per cell: smoke, not
metrics. Key facts judged by Claude by hand, strict, as in 3a.5: a fact counts only if the
summary, likely_cause or a kept claim states it; partials in brackets; next_checks count only for
a "what steps" fact. Transcripts in scratchpad/phase3/a6/ (first round in a6/v1/).

Prompt/graph change from the first round (dev transcripts, not the demo): gpt-oss spent 3 to 4 of
8 calls re-reading documents it had read (duplicates of part 1, or part 2 of a one-part
document). Root cause: the part count ("INC-0109 part 1 of 1") was visible only on the turn
after the read; later turns showed "-> 3 items" and a 100-character index line per item, so the
document looked unread. Now each finished call keeps its note in "calls so far", and the gather
prompt says the index is cut short but every item is kept in full: do not read a document
again. Effect: gpt-oss dev tool success 23/40 -> 21/34 (fewer calls, still re-reads); Gemini
re-reads less. Not fixed further.

| run | model | tool calls ok | key facts | forbidden / contradicting | id fails per pass | quote fails per pass | loops | LLM calls | tokens prompt / completion | max call (actual) | wall s | 429 wait s | cost $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Q063 | gpt-oss (OR) | 4/8 | 2/3 (1) | 0 | 0,0 | 2,0 | 1 | 13 | 23,408 / 8,501 | 5,130 | 36.7 | 0.0 | 0.00817 |
| Q063 | Gemini | 4/6 | 2/3 | 0 | 0,0,0 | 3,3,3 | 2 | 13 | 24,328 / 1,430 | 2,865 | 17.4 | 0.0 | 0 |
| Q074 | gpt-oss (OR) | 4/8 | 2/2 | 0 | 0,0 | 2,0 | 1 | 12 | 20,397 / 5,149 | 3,740 | 21.1 | 0.0 | 0.00577 |
| Q074 | Gemini | 2/2 | 2/2 | 0 | 0 | 0 | 0 | 5 | 7,283 / 470 | 2,358 | 72.0 | 62.8 | 0 |
| H007 | gpt-oss (OR) | 6/8 | 0/3 | 0 | 0,0 | 6,0 | 1 | 13 | 24,747 / 10,959 | 4,993 | 41.4 | 0.0 | 0.00675 |
| H007 | Gemini | 6/8 | 0/3 (3) | 0 | 0,0,0 | 1,1,1 | 2 | 12 | 24,998 / 1,468 | 3,294 | 16.7 | 0.0 | 0 |
| Q017 | gpt-oss (OR) | 5/8 | 1/2 | 0 | 0 | 0 | 0 | 10 | 15,253 / 3,451 | 3,174 | 18.7 | 0.0 | 0.00421 |
| Q017 | Gemini | 6/8 | 1/2 | 0 | 0,0,0 | 2,1,1 | 2 | 12 | 23,066 / 1,311 | 2,761 | 105.6 | 87.4 | 0 |
| Q053 | gpt-oss (OR) | 2/2 | 1/2 (1) | 1 (ALTER TABLE lock; INC-0091 was no migration) | 0 | 0 | 0 | 5 | 7,053 / 2,792 | 3,499 | 16.4 | 0.0 | 0.00262 |
| Q053 | Gemini | 2/2 | 1/2 (1) | 0 | 0 | 0 | 0 | 5 | 6,506 / 582 | 1,990 | 11.2 | 0.0 | 0 |
| demo | gpt-oss (OR) | 7/8 | 0/9 | 0 | 0,0 | 1,0 | 1 | 11 | 22,607 / 4,165 | 3,922 | 23.0 | 0.0 | 0.00566 |
| demo | Gemini | 7/7 | 0/9 (2) | 0 | 0,0,0 | 1,1,1 | 2 | 11 | 23,789 / 1,589 | 3,074 | 40.9 | 24.1 | 0 |

Before vs after read_document (key facts; before = the 3a.5 table, gpt-oss on Groq direct):

| | gpt-oss dev (12) | Gemini dev (12) | gpt-oss demo (9) | Gemini demo (9) |
|---|---|---|---|---|
| 3a.5, no read_document | 1 (3 partial) | 2 (1) | 1 | 2 |
| 3a.6, read_document | 6 (2) | 6 (4) | 0 | 0 (2) |
| contradictions (dev), before / after | 1 / 1 | 2 / 0 | | |

- The dev gain is real for both models and comes from reading: Q063's journald cause and fix, and
  Q074's fix, sit in chunks search never showed.
- Stop condition (gpt-oss < 5 of 12) not met: 6 of 12. Part B goes ahead.
- Gemini is not clearly better on key facts (6 vs 6). gpt-oss stays the agent. Notes for Prince:
  Gemini found the demo mechanism again (pgbouncer rejected pool_mode "transction"; partial, no
  exit status) while gpt-oss stopped at "payments-pgbouncer degraded" again; gpt-oss produced no
  valid diagnosis on H007 (first draft cut at 2,500 completion tokens, the post-loop draft and
  its retry returned empty content after about 2,200 tokens of reasoning); Gemini's three Q063
  claims all failed the quote check and were dropped (the summary still stated two facts).
- Q053 (gpt-oss): it read RB-028 and took RB-028's example incident (a migration lock, 14:02) as
  the 15 October one. The quote check passed, since the words are in RB-028: citation accuracy (b)
  is the judge's job.
- Draft evidence: H007 dropped 8 to 13 evidence lines from the draft prompt (both models), the
  demo on gpt-oss 8 to 10; the other runs dropped none. Read chunks (about 1,000 chars each) use
  the 6,000-token budget fast.
- Cost: gpt-oss dev mean 18,172 prompt + 6,170 completion tokens, $0.0055 per run (reported by
  OpenRouter); about $0.55 per 100-question config, $1.10 for verify on plus off. Spend so far:
  $0.0618 (OpenRouter account usage, equals the summed reported costs: two rounds of 6 runs plus
  two one-word checks). Gemini: 47 requests for the 5 dev runs, 91K tokens, free.

**Errors and fixes**
- An empty `OPENROUTER_API_KEY=` (as in .env.example) is an empty SecretStr, not None, so the
  client sent `Bearer ` with no key. Root cause: the check was `key is None`. Now an empty value
  counts as unset, for every provider. Found by a test.

## 2026-10-12: Phase 3b, generation eval on dev (verify on vs off), judge, hand-label sheet

**Built**
- `eval/generation_eval.py`, `make gen-eval`: the graph on all 100 dev questions at as_of
  2026-10-06T14:52:00Z (after every corpus incident, so any past window is reachable), verify on
  then off, EVAL_CONCURRENCY 3 (new setting), one Client per run. Three resumable stages with
  progress files in .cache/gen_eval/: agent, judge, score. New spend (uncached calls' reported
  cost, both configs) is checked before each run starts against --max-spend (default $4.50).
  Results: eval/results/dev_generation_verify_{on,off}.json (commit, uncommitted files, config,
  models, per-question scores and judge labels; unchanged content is not rewritten), an
  eval_runs row with eval_results per question, and section (c) of docs/results.md.
- Metrics are (numerator, denominator) per question, pooled per slice, n beside every value.
  Paired bootstrap: resample questions (10,000, seed 20261003), recompute both configs' pooled
  ratios on the same resample, CI of the difference.
- `eval/judge.py`: one call per answer, strict JSON (per-claim supported, per-key-fact present,
  faithfulness 1-5, premise_corrected, unrecorded_respected), temperature 0, cached by prompt
  hash, PROMPT_VERSION judge-v1 inside the prompt. It sees the question, key facts, diagnosis and
  the cited evidence texts only (an ungathered id is shown as not gathered). A verdict missing a
  claim or fact gets one retry. RateLimiter: 4 s between real requests (15 RPM) and 500 per UTC
  day counted in a file; cache hits never wait or count.
- `make judge-sheet` -> eval/judge_validation.md: 30 verify-on answers, seeded, largest-remainder
  stratified by difficulty x author; question, summary, cause, each claim with its cited evidence
  text, key facts, blanks for SUPPORTED / PRESENT; the judge's labels are not shown.
  `make judge-agreement`: percent agreement and Cohen's kappa (claims, key facts, all).

**Errors and fixes (root cause first)**
- Smoke run, Q003: gpt-oss answered its first gather turn with the log window in prose and no
  tool call; the loop read any reply without a call as done, so it drafted from no evidence.
  Now a reply without a call ends gather only if it says done or calls were already made;
  otherwise one nudge, on the next turn only (a test caught my first version leaving the nudge
  on every later turn). No run in the full eval needed it.
- `--limit 3` wrote the named result file (3 of 3 "questions" looked complete). A --limit run
  now never writes it; the smoke rows were deleted so all 100 ran on one code version.
- The eval log looked stalled for 6 minutes: Python block-buffers print into a file; the
  progress file showed 70 runs done. `make gen-eval` now sets PYTHONUNBUFFERED.
- Verify off made no new LLM calls: its triage, gather and first draft prompts equal verify on's,
  so all came from the cache. That is the right pairing (off = on's first draft, unlooped), but
  its latency is replay time, so the ledger reports it as not measured.

**Results (dev, n = 100, one run per question; judge metrics unvalidated until Prince's labels)**

| metric | verify on | verify off | on - off [95% CI] | claimable |
|---|---|---|---|---|
| citation (a), claims | 1.000 (270) | 0.763 (333) | +0.218 [+0.164, +0.279], n 98 | yes |
| citation (b), claims (judge) | 0.974 (270) | 0.964 (333) | +0.011 [-0.012, +0.037], n 98 | no |
| faithfulness 1-5 (judge) | 4.77 (100) | 4.77 (100) | +0.000 [-0.130, +0.130] | no |
| completeness, key facts (judge) | 0.732 (254) | 0.783 (254) | -0.051 [-0.106, +0.000] | no |
| tool-call success, calls | 0.677 (601) | 0.778 (473) | -0.101 [-0.146, -0.061] | not a verify effect (see 3b.1) |
| premise corrected (judge) | 6/7 | 6/7 | | |
| unrecorded respected (judge) | 2/6 | 2/6 | | |
| verify-loop rate | 0.46 (100) | 0 | | |
| no diagnosis (fallback) | 2/100 | 0/100 | | |

- Citation (a) with verify on is 1.000 by construction: verify drops every claim that fails this
  check. The measured effect is the off column: 23.7% of first-draft claims cite something not
  gathered or quote words not in the cited item. CIs exclude 0 in every slice.
- Citation (b): the judge calls 96% of unverified claims supported, so most quote failures are
  inexact quotes of real support, not invented support. Verify's gain is exactness, not (per an
  unvalidated judge) support.
- Completeness leans down with verify (-0.051, CI touching 0): dropped claims take key facts
  with them, and 2 looped runs ended with no valid diagnosis. Not claimable.
- Tool success drops with verify: the extra gather turns mostly re-read documents. In verify on,
  175 of 327 read_document calls were blocked duplicates (verify off: 92 of 232); query_logs ran
  8 times in 100 runs (dev questions are about past incidents and the agent reads documents).
- Unrecorded respected 2/6: answers invent the part the corpus does not record. Premise 6/7.
- Slices (verify on): completeness exact-token 0.798 (109 facts, n 45), multi-doc 0.675 (77,
  n 28), paraphrase 0.691 (68, n 27); claude-authored 0.758 (198, n 80), gemini-authored 0.643
  (56, n 20); false premise 0.591 (22, n 7). Small n: no slice gap here is claimed.
- Judge vs my strict Part A labels on the 5 confirm questions (same cached answers): 10 of 12
  key facts agree; both disagreements are facts I scored partial and the judge scored present.
  A hint the judge is lenient on partials; Prince's 30 labels are the real check.

**Cost, tokens, time**
- Verify on, per run (mean of 100): 15,643 prompt + 4,248 completion tokens, 9.5 LLM calls,
  6.0 tool calls, $0.0044 (OpenRouter-reported). Verify off: 11,483 + 2,766, 7.4 calls, 4.7 tool
  calls, $0.0030 (original cost of its cached calls; no new spend).
- New spend for the eval: $0.4037. OpenRouter account total after Phases 3a.6 and 3b: $0.4732
  (Part A $0.0618, three pre-fix smoke runs about $0.008, eval $0.4037), $4.53 left.
- Latency, verify on (Mac, concurrency 3, no 429 waits): end to end p50 12.0 s, p95 25.9 s
  (n 100); LLM call p50 853 ms, p95 4,451 ms (n 875); tool call p50 3.9 ms, p95 124 ms (n 601).
- Wall time: verify-on agent 7.4 min, its judge 6.5 min, verify-off agent 6 s (cache), its
  judge 3.0 min. Judge: 147 real Gemini requests today (+3 smoke), the rest cache hits (the 54
  runs that never looped give identical answers in both configs).

**Open, for Prince**
- Fill eval/judge_validation.md (165 blanks: claims and key facts of 30 answers), then
  `make judge-agreement`.
- gpt-oss re-reads documents despite the part count and the prompt rule; a duplicate
  read_document could return a pointer to the earlier result instead of an error, or the gather
  prompt could keep read documents in full. Not changed: it would change the agent after this
  eval.

## 2026-10-12: Phase 3b.1, agent fixes from the dev eval

Each fix is justified by the 3b dev transcripts or by general on-call practice; none by the demo.

1. **Read list.** 3b: 175 of 327 read_document calls (verify on) were repeats of a read already
   made, despite the part count and a prompt rule. Root cause: the model had no list of what it
   had read; older results showed only as 100-character index lines. State now keeps `reads`
   (doc id, part, part count, evidence ids), shown on every gather turn as "Already read, in the
   evidence in full". A repeat (same document and part, however written: `INC-0109#2`, an
   explicit `part: 1`) is still refused and counted as a failed call, and the refusal now names
   the evidence ids to cite. The list survives a verify loop.
2. **Look before reading.** 3b: query_logs ran 8 times in 100 dev runs; the agent answered from
   documents alone. General on-call practice is to look at the system first (status, then errors
   and warnings around the reported time) and read runbooks second. When triage names a service,
   gather's first pass now runs get_service_status for it and query_logs for it over the triage
   window at level WARN and above, before the model's own calls; they count toward the 8-call cap
   and are shown in full on the first turn. Deterministic, so it cannot be skipped. Setting
   `AGENT_FIRST_LOOK` (default true); unit tests that are not about it turn it off.
3. **Not recorded.** 3b: unrecorded respected 2/6; the answers filled in what the corpus does not
   record. Draft prompt: if the question asks for something the evidence does not state (a cause,
   a time, a number, a name, a reason), the summary says it is not recorded in the evidence;
   never infer or guess it.
4. **Reasoning budget.** 3b: 2 of 100 drafts returned no diagnosis (H007 in 3a.6: about 2,200
   tokens of reasoning, empty content). OpenRouter's `reasoning.effort`
   (openrouter.ai/docs/use-cases/reasoning-tokens); for gpt-oss the models API lists efforts
   low, medium, high, default medium, reasoning mandatory, and reasoning counts against
   max_tokens. The client takes `reasoning_effort` (sent as `reasoning.effort` on OpenRouter,
   `reasoning_effort` on Groq, ignored elsewhere; in the cache key only when set). Drafts run at
   low; any structured call with empty content is retried once at low (one level down for
   triage, which runs at the default; the draft is already at gpt-oss's floor). The 2,500-token
   draft cap is unchanged so the draft keeps its evidence room. Live check: "Is 17 prime?" took
   54 completion tokens at the default, 16 at low.
5. **Verify on/off reading.** Verify off is the same trajectory's first draft: its triage, gather
   and first-draft prompts equal verify on's (the same cached calls), so the comparison is
   paired by construction. Verify on then may loop back to gather, which adds tool calls; the
   tool-call success difference (-0.101) is therefore not a verify effect: verify off replays
   the first draft and skips later gather rounds. The ledger now flags only citation (a) as
   claimable; the other comparison rows are context.

Tests: first look runs status then WARN logs before the model's calls, not without a named service,
not again on a verify loop; repeated read refused with ids (two spellings), list shown and kept
across a loop; draft at low effort, empty reply retried at low (draft and triage); reasoning
effort reaches openrouter and groq only, and is in the cache key only when set; ledger flags.

## 2026-10-13: Phase 3b.1, dev generation results after the fixes

Same 100 dev questions, same judge (gemini-3.5-flash-lite, judge-v1), concurrency 3, code
8388fbb. Old = Phase 3b (code b25000c). Verify on unless marked.

| | old | new | new - old, paired [95% CI] |
|---|---|---|---|
| tool-call success | 0.677 (601 calls) | 0.899 (694) | +0.222 [+0.181, +0.261] |
| read_document repeats refused / reads | 124 / 327 | 32 / 185 | |
| query_logs calls (runs using it) | 8 (6 of 100) | 138 (100 of 100) | |
| unrecorded respected | 2/6 | 3/6 | n 6: no claim |
| premise corrected | 6/7 | 5/7 | n 7: no claim |
| no-diagnosis runs | 2/100 | 0/100 | |
| first-draft citation (a) failures (verify off) | 0.237 (333 claims) | 0.328 (341) | (a) -0.091 [-0.170, -0.014] |
| citation (b), verify on | 0.974 (270) | 0.954 (238) | -0.015 [-0.051, +0.019], n 89 |
| completeness, verify on | 0.732 (254 facts) | 0.657 (254) | -0.075 [-0.154, +0.004] |
| completeness, verify off | 0.783 (254) | 0.704 (226, n 90) | -0.084 [-0.162, -0.004], n 90 |
| faithfulness, verify on | 4.77 | 4.59 | -0.18 [-0.41, +0.04] |
| cost per run, verify on | $0.0044 | $0.0037 | |
| tokens per run, verify on | 15,643 / 4,248 | 15,815 / 2,891 | |
| end to end p50 / p95, verify on | 12.0 / 25.9 s | 8.4 / 13.4 s | |

Verify on vs off (new): citation (a) +0.298 [+0.242, +0.359], n 91, claimable; citation (b)
-0.016 [-0.045, +0.009]; completeness -0.040 [-0.082, +0.000]; faithfulness -0.18 [-0.36,
-0.02] (context only, see 3b.1 item 5).

- What worked: tool success, repeats (124 -> 32), logs (every run now looks at logs), no
  fallback diagnoses, cheaper and faster runs (low draft effort: completion tokens -32%).
- What got worse: first drafts quote less exactly (failures 23.7% -> 32.8%, CI excludes 0) and
  completeness fell (verify off CI excludes 0 by a hair; verify on touches 0). Four changes went
  in together, so the cause is not separable from this run. Candidates: the low draft effort
  (copying quotes and stating facts exactly may need the reasoning), first look taking 2 of the
  8 calls (reads fell from 327 to 185; runs at the cap 58 -> 64), and more evidence competing for
  the draft prompt (drafts that dropped evidence lines: 50 of 163 -> 62 of 203). An ablation
  (DRAFT_EFFORT back to the default; AGENT_FIRST_LOOK off) would separate them at about $0.35
  per config. Not run: Prince decides.
- Correction to the 3b entry: of 327 read_document calls, 124 were refused repeats and 51 asked
  for a part that does not exist (175 failed reads in all), not 175 repeats.
- Gemini's daily quota ran out while judging verify off: 10 answers unjudged (H007, H008, H009,
  H015, H018, H023, H029, H034, H039, H040; retry-after about 23 h), so verify off's judged
  metrics have n 90 and its author=gemini slice n 10. Root cause: the judge counted requests per
  UTC day (147 on the 12th, 165 on the 13th), but Google resets at midnight Pacific, so both
  counts fell into one quota day, along with Gemini calls the counter cannot see (Gemini as the
  agent in 3a.6). The limiter now counts Pacific days (f7cb0b3). `make gen-eval` judges the 10
  once the quota resets; everything else is cached.
- Demo smoke after the fixes (one run, report only): 0/9 key facts (1 partial: it names the
  misspelled pool_mode as why pgbouncer failed to load, without "transction" or exit status 1,
  and without separating pgbouncer-01 from -02), 0 forbidden claims, logs queried twice (first
  look on payments-api WARN+, then payments-pgbouncer ERROR), 8/8 calls ok, 1 loop, $0.0038. In
  3a.6 gpt-oss stopped at "payments-pgbouncer degraded".
- eval/judge_validation.md regenerated from the new verify-on answers: the same 30 questions
  (seeded, stratified), 160 blanks.
- Spend: eval $0.3290 + demo $0.0038 + checks; OpenRouter account total $0.8056 (cap $2 for this
  phase, $5 credit). Time: verify-on agent 4.6 min, judge 6.6 min; verify-off agent 4 s (cache),
  judge 4.1 min until the quota stopped it.

## 2026-10-13: Phase 3b.2, ablation of the 3b.1 changes

**Why.** 3b.1 bundled four fixes (read list, first look, not-recorded rule, low draft effort) into
one change and compared it with 3b as a whole, so its before/after differences could not be
attributed to any one fix: the comparison was confounded. This step changes one thing at a time.

**Design.** Three arms, verify on, all 100 dev questions, each one change from 3b.1:
A draft reasoning effort back to the provider default (medium); B first look off; C first look
on with a tool-call cap of 10 (was 8). Each arm's `_off` config replays its own first draft
from the cache (free), giving first-draft citation (a). New deterministic metric, no judge:
gold-doc coverage, `gold_all` (share of runs that gathered or read every gold doc) and `gold_frac`
(mean fraction of gold docs gathered per run), from the documents in the run's evidence map.
- Runs before 3b.2 did not record gathered documents, so 3b.1 was replayed with the current code
  and the old 3b run with its own code (b25000c, in a git worktree), both from the response
  cache with the OpenRouter key blanked (a miss would fail, not spend): 400 runs, every call
  cached, every diagnosis identical to the original. Their result files now carry gold coverage;
  a pure replay keeps the measured run's latency and spend (fix 003dea0, tested).
- Judge: the 300 arm answers and the 10 verify-off answers 3b.1 left unjudged, after Gemini's
  quota reset at midnight Pacific: 298 real requests. The limiter's counter file was reset first:
  its entries had been keyed by UTC day before f7cb0b3 and would have counted 165 requests
  against the new Pacific day.
- Report: eval/results/dev_generation_ablation.md (`uv run python -m eval.generation_eval
  ablation`), result files dev_generation_arm_{a,b,c}_{on,off}.json.

**Results** (verify on; value with denominator; differences paired over questions, 95% CI;
* = CI excludes 0)

| metric | old 3b | 3b.1 | A effort default | B no first look | C cap 10 |
|---|---|---|---|---|---|
| completeness (254 facts) | 0.732 | 0.657 | 0.665 | 0.693 | 0.630 |
| gold_all (100 runs) | 0.710 | 0.670 | 0.660 | 0.800 | 0.760 |
| gold_frac | 0.861 | 0.821 | 0.815 | 0.900 | 0.883 |
| first-draft citation (a) failure | 0.237 (333) | 0.328 (341) | 0.222 (338) | 0.367 (327) | 0.323 (334) |
| citation (b) | 0.974 (270) | 0.954 (238) | 0.973 (296) | 0.972 (250) | 0.962 (238) |
| tool-call success | 0.677 (601) | 0.899 (694) | 0.914 (659) | 0.854 (643) | 0.851 (824) |
| reads per run (ok) | 1.52 | 1.39 | 1.30 | 1.61 | 1.54 |
| query_logs per run (runs using) | 0.08 (6) | 1.38 (100) | 1.39 (100) | 0.22 (12) | 1.41 (100) |
| cost per run | $0.0044 | $0.0037 | $0.0043 | $0.0042 | $0.0043 |
| end-to-end p95 | 25.9 s | 13.4 s | 17.5 s | 17.7 s | 16.3 s |

Versus 3b.1: A first-draft failure -0.107 [-0.179, -0.039]*; B gold_all +0.130 [+0.040, +0.220]*,
gold_frac +0.079 [+0.027, +0.134]*, tool success -0.045 [-0.074, -0.017]*; C gold_all +0.090
[+0.010, +0.180]*, gold_frac +0.062 [+0.017, +0.115]*, tool success -0.048 [-0.074, -0.023]*. No
arm's completeness differs from 3b.1 (A +0.008, B +0.035, C -0.028, all CIs span 0).
Versus old: every arm keeps a tool-success gain (A +0.236*, B +0.177*, C +0.174*); A's first-draft
failure is back at old's level (-0.015, CI spans 0); C's completeness is lower (-0.102 [-0.187,
-0.020]*); B's gold_all is higher (+0.090 [+0.010, +0.170]*).

**Findings**
- The low draft effort caused the first-draft quote regression: undoing it alone (A) removes it
  (claimable). It did not change completeness or coverage.
- First look costs gold-doc coverage under the 8-call cap: its 2 calls crowd out searches and
  reads. Removing it (B) or giving it 2 more calls (C) raises coverage (both claimable).
- The completeness drop that started this step does not hold up. With the 10 missing verify-off
  answers now judged, 3b.1 minus old is -0.071 [-0.145, 0.000] for verify off (n 100; it was
  -0.084 [-0.162, -0.004] on n 90) and -0.075 [-0.154, +0.004] for verify on: neither CI
  excludes 0, and no single change moves completeness detectably at n 100.
- Coverage does not translate into judged completeness here (C has the second-best coverage and
  the lowest completeness); the judge is still unvalidated (Prince's 30 labels pending).
- Dev questions are about past, documented incidents; logs rarely carry their key facts, so dev
  cannot show the value of looking at logs first. That is a limit of the dev set, not evidence
  against first look on live incidents.

**Recommendation (Prince decides; nothing locked, hand-label sheet not regenerated).** Arm B:
first look off, everything else as 3b.1. It has the best coverage (gold_all 0.800 vs 0.670,
claimable) and the highest completeness of the new configs (not claimable), keeps most of the
tool-success gain over old (+0.177, claimable), and is cheaper than C. Costs: tool success -0.045
vs 3b.1 (claimable) and first-draft quote failures 0.367 (above old, claimable) from the low draft
effort that A shows is the cause. Untested and not claimed: B with A's default draft effort is
the likely better combination (about $0.40 to test). If live-incident log checks matter more
than dev coverage, C is the alternative.

**Spend.** Arms $0.9909 new (A $0.2374, B $0.3785, C $0.3750; `_off` configs and all replays $0).
OpenRouter total $1.7965 of $5 (this step's cap $1.50). Judge: 298 Gemini requests.

## 2026-10-13: Phase 3, log-first only for live incidents

Log-first is right where the evidence is in the logs and costly where it is not. Dev questions ask
about past incidents that have write-ups; there the 2 fixed first-look calls crowded out searches
and reads under the 8-call cap, and turning log-first off raised gold-doc coverage (3b.2, arm B
vs 3b.1: gold_all +0.130 [+0.040, +0.220], gold_frac +0.079 [+0.027, +0.134]). A live incident has
no write-up yet, so its evidence is the current status and logs; there log-first is standard
on-call practice (3b.1 item 2). So triage now labels `incident_time`: live when the question
describes a current symptom and names no past date or past incident, past otherwise (required
field in the triage JSON). The first look runs only when live; `AGENT_FIRST_LOOK` stays the master
switch. The demo is not the reason: it is the only live question we have, and it stays a check.
Tests: a past question skips the first look (its first turn shows 0 of 8 calls); a live question
runs status then WARN logs; the triage schema requires incident_time (live | past).

## 2026-10-13: Phase 3b.3, arm D, the locked agent config, final dev generation results

**Arm D** = 3b.1 with the default draft reasoning effort (arm A), log-first only for live
incidents (triage incident_time), tool-call cap 8. Verify on, 100 dev questions, judged; the
verify-off first draft from the cache. New spend $0.4286 (cap $0.75).

| metric | 3b.1 | A | B | D | D - B [95% CI] | D - A | D - 3b.1 |
|---|---|---|---|---|---|---|---|
| completeness (254) | 0.657 | 0.665 | 0.693 | 0.669 | -0.024 [-0.110, +0.058] | +0.004 [-0.077, +0.084] | +0.012 [-0.072, +0.094] |
| gold_all (100) | 0.670 | 0.660 | 0.800 | 0.690 | -0.110 [-0.190, -0.030]* | +0.030 [-0.060, +0.120] | +0.020 [-0.080, +0.120] |
| gold_frac | 0.821 | 0.815 | 0.900 | 0.845 | -0.055 [-0.105, -0.009]* | +0.030 [-0.020, +0.082] | +0.023 [-0.030, +0.078] |
| first-draft citation (a) failure | 0.328 | 0.222 | 0.367 | 0.209 | -0.158 [-0.239, -0.076]* | -0.013 [-0.079, +0.056] | -0.120 [-0.197, -0.043]* |
| citation (b) | 0.954 | 0.973 | 0.972 | 0.965 | -0.009 [-0.037, +0.018] | -0.007 [-0.035, +0.018] | +0.009 [-0.025, +0.043] |
| tool-call success | 0.899 | 0.914 | 0.854 | 0.862 | +0.008 [-0.021, +0.037] | -0.052 [-0.086, -0.018]* | -0.037 [-0.071, -0.006]* |
| reads per run | 1.39 | 1.30 | 1.61 | 1.58 | | | |
| query_logs per run (runs) | 1.38 (100) | 1.39 (100) | 0.22 (12) | 0.31 (21) | | | |
| cost per run | $0.0037 | $0.0043 | $0.0042 | $0.0043 | | | |
| end-to-end p95 | 13.4 s | - | - | 59.0 s | | | |

n = 100 for every difference except citation (b) (86 to 99, questions with claims judged in
both). End-to-end latency counts only runs made with no cache hits (fix e69bfe8 and 3f45b35):
A, B and C reused 3b.1's cached triage, so theirs is not measured; D's 59.0 s is real.

**Past/live split.** Triage labelled 90 of 100 dev questions past and 10 live: Q056, Q070, Q082,
Q087, Q098, Q103, Q104, Q105, Q118, Q132. All ten describe a current symptom and name no past
date or incident ("ledger instance cpu is climbing and the hikari pool is near full, is bumping
maximumPoolSize a good idea mid-incident?"), so the label follows the definition; the >= 95%
expectation was wrong for this dev set, about a tenth of which is phrased as live while its gold
is a past write-up.

**Lock rule.** D needed (1) no claimable gold-doc loss vs B: failed (gold_all -0.110, gold_frac
-0.055, both CIs exclude 0); (2) no claimable first-draft citation (a) loss vs A: passed (-0.013,
CI spans 0); (3) >= 95% of dev questions labelled past: failed (90%). So **B with the default
draft reasoning is locked**: `AGENT_FIRST_LOOK=false` (config.py), `DRAFT_EFFORT = None` (the
provider default, medium for gpt-oss; agent/graph.py), cap 8, verify on, quote check on, read
list, not-recorded rule, empty-reply retry at low, agent openrouter:openai/gpt-oss-120b pinned to
Groq, judge gemini:gemini-3.5-flash-lite (judge-v1). The live-only first look stays in the code
behind the switch, for live-incident work later. Locked in commit da5bd8c.
- The locked config was never run before this step (B ran with low draft effort and the old
  triage). Measured now: gold_all 0.720 vs B 0.800, -0.080 [-0.160, +0.000]; gold_frac 0.857 vs
  0.900, -0.043 [-0.092, +0.003]; first-draft failure 0.214 vs A 0.222, -0.008 [-0.076, +0.063];
  none claimable. B's coverage gain did not fully carry over (point estimates lower, CIs touch 0).
- On the 90 questions labelled past, the locked config and D make identical prompts (D's first
  look runs only for live), so 90 of its runs came from D's cache: new spend $0.0412.

**Tuning on dev stops here.** Several configs (3b, 3b.1, arms A to D, the locked one) were
compared on the same 100 dev questions and the locked one was chosen from them, so dev numbers
are slightly optimistic. Resume numbers come only from the Phase 5 test run.

**Final dev results, locked config** (eval/results/dev_generation_verify_{on,off}.json, commit
e69bfe8; docs/results.md section (c))

| metric | verify on | verify off | on - off [95% CI] |
|---|---|---|---|
| citation (a) | 1.000 (285 claims) | 0.786 (322) | +0.207 [+0.151, +0.265], n 99, claimable |
| citation (b), judge | 0.972 (285) | 0.975 (239, n 79) | -0.006 [-0.016, -0.000], n 79, context |
| faithfulness 1-5 | 4.74 (100) | 4.76 (79) | -0.025 [-0.101, +0.038], context |
| completeness | 0.665 (254 facts) | 0.658 (193, n 79) | -0.010 [-0.046, +0.025], context |
| tool-call success | 0.853 (577) | 0.875 (479) | not a verify effect |
| gold_all / gold_frac | 0.720 / 0.857 | 0.630 / 0.807 | |
| premise corrected / unrecorded respected | 3/7, 5/6 | | |
| verify loops / no diagnosis | 41/100, 1/100 | | |

- 21 verify-off answers are unjudged: Gemini's daily quota ran out (retry-after about 4.9 h), so
  verify off's judged metrics have n 79. `make gen-eval` judges them after the reset; everything
  else is cached. Only citation (a) is a claim about verify, and it is complete.
- Premise corrected fell to 3/7 (6/7 in 3b): n 7, not claimable, but worth watching.
- eval/judge_validation.md regenerated from the locked verify-on answers: same 30 questions,
  165 blanks.

**Demo smoke, locked config** (one run, a check only): triage labelled it live (first look is
off in the locked config, so no automatic calls); 0/9 key facts (1 partial: an invalid pool_mode
made pgbouncer unable to load its config and stay down, without "transction", exit status 1 or
which bouncer); 0 forbidden claims; logs queried once (payments-pgbouncer ERROR, chosen by the
model); 7/8 calls ok (one repeated search refused); 0 loops; $0.0040.

**Spend.** This step $0.4786 by the OpenRouter account (D $0.4286, locked $0.0412, demo
$0.0040 from recorded calls; $0.0048 unattributed, possibly provider-rejected outputs, which
return no usage). OpenRouter total $2.2751 of $5.

## 2026-10-13: Phase 3, judge validation (Prince's 30 hand labels)

Prince labelled the 30 seeded, stratified verify-on answers of the locked config
(eval/judge_validation.md, commit e69bfe8 answers): 164 items, every blank marked (108 yes, 56 no),
no unknown ids, no duplicates. Saved exactly as given in eval/prince_labels.txt; the labels are
never changed. `make judge-agreement` compares them with the judge's verdicts on the same answers
and writes eval/results/judge_agreement.json; docs/results.md section (c2). Kappa CIs: percentile
bootstrap resampling questions (items of one answer stay together), 10,000 resamples, seed
20261003.

| items | n items (questions) | agreement | Cohen's kappa [95% CI] | Prince yes | judge yes |
|---|---|---|---|---|---|
| claim support | 87 (29) | 0.874 | -0.053 [-0.103, +0.000] | 0.908 | 0.966 |
| key-fact presence | 77 (30) | 0.714 | +0.471 [+0.283, +0.667] | 0.377 | 0.662 |
| both | 164 (30) | 0.799 | +0.494 [+0.327, +0.646] | 0.659 | 0.823 |

2x2 (Prince x judge): claims yes/yes 76, yes/no 3, no/yes 8, no/no 0; key facts yes/yes 29,
yes/no 0, no/yes 22, no/no 26.

- Claims: 87% agreement but kappa about 0. The judge marked 84 of 87 claims supported and never
  agreed with any of Prince's 11 "no"s (no/no = 0), so its agreement is what saying yes to nearly
  everything gets by chance. The judge cannot detect unsupported claims here.
- Key facts: moderate. Every disagreement is Prince no / judge yes (22, reverse 0): the judge is
  lenient on partial or vaguer statements, as the 3a.6 spot check (10 of 12) suggested.
- "Both" (0.494) is higher than either part: pooling two item types with different yes rates
  inflates kappa; the per-metric numbers are the ones to use.
- Splits (n small, no slice claims): claims kappa about 0 in every slice; key facts by author
  claude 0.541 (59 items, 24 q) vs gemini 0.182 (18, 6); by difficulty exact-token 0.429 (32, 13),
  multi-doc 0.476 (22, 8), paraphrase 0.504 (23, 9).
- Disagreements: 33 of 164; Prince no / judge yes 30 (claims 8, key facts 22), Prince yes / judge
  no 3 (claims Q138 C3, Q043 C2, Q043 C3). Full list in the JSON.
- Sensitivity, on these 30 answers: citation (b) 0.966 by the judge vs 0.908 by Prince (87
  claims, 29 q); completeness 0.662 by the judge vs 0.377 by Prince (77 facts, 30 q). The judge's
  completeness is far more generous than Prince's standard.

Prince marked 'no' when unsure, so key-fact presence is a strict standard; the disagreement
pattern is reported, not corrected.

**Rule for using the judged metrics** (per metric, by kappa against Prince's labels):
- kappa below about 0.4: report that judged metric as unreliable and lean on deterministic
  metrics;
- 0.4 to 0.6: moderate; report it with the agreement number beside it;
- above 0.6: report it normally, with the agreement number beside it.

Applied: **citation (b)** (claim support, kappa -0.05): unreliable; citation (a) (deterministic)
and gold-doc coverage carry the citation story. **Completeness** (key-fact presence, kappa 0.47):
moderate; report it with "71% agreement, kappa 0.47 against a stricter human (judge 0.66 vs human
0.38 on 30 answers)" beside it. **Faithfulness, premise corrected, unrecorded respected**: no hand
labels, so not validated; report them as unvalidated. The 3b.2 and 3b.3 decisions already rested
on deterministic metrics (gold-doc coverage, first-draft citation (a)), which this supports.

## 2026-10-13: Phase 3 close-out (verify-off judging, judge verdict)

- `make gen-eval` (everything else cached, $0 new agent spend) judged 8 of the 21 unjudged
  verify-off answers (Q073, Q117, Q118, Q125, Q126, Q132, Q138, Q146); Gemini returned 429 again
  for the other 13 (retry-after about 3.9 h). Verify off's judged metrics now have n 87:
  citation (b) 0.971 (275 claims), faithfulness 4.747, completeness 0.665 (215 facts). On - off
  completeness -0.019 [-0.056, +0.018], faithfulness -0.011 [-0.080, +0.046]: no claimable
  difference, as before. Still unjudged: Q156, Q158, Q159, H001, H002, H007, H008, H009, H018,
  H026, H029, H030, H038; `make gen-eval` finishes them after the quota resets.
- **Judge verdict** (from the judge validation entry): the judge **failed validation on claim
  support** (kappa -0.05) and is **moderate on completeness** (kappa 0.47, 71% agreement, more
  lenient than Prince). So citation (b) is context only, never a headline number. **Resume
  claims rest on deterministic metrics**: citation (a), gold-doc coverage, tool-call success,
  and (Phase 4) injection attack success by string/regex check.

## Standing rules (write-ups)

- **Team naming:** a write-up may name a team only if `services.toml` lists it as owner of the row's
  cause or symptom service, or a runbook the row cites (not a post-dated one) names it. Enforced by the
  validator's `teams` check (`team_sources` in `scripts/corpus/incidents.py`).

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
- **F3:** the `pgbouncer` systemd unit is `Type=simple`, so `systemctl restart` exits 0 even if pgbouncer dies seconds later. Upstream's shipped unit differs: `etc/pgbouncer.service` at `pgbouncer_1_22_0` is `Type=notify`.
- **F4:** the rollout tool logs to each host's journal under the identifier `rollout`.
- **F5:** a rollout-tool run can be scoped to a subset of hosts (e.g. a canary-only run).
- **F6:** the pgbouncer systemd unit has no Restart= setting (systemd default: no restart), so a failed start stays down until someone acts.
- Demo: 2026-10-06, a run scoped to payments-pgbouncer-01. The config error is a misspelled `pool_mode` (`transction`), verified from 1.22 source in the 2026-10-11 PgBouncer entry.

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

**Batch 7 cleanup facts** (approved 2026-10-09; sidecar `facts` for rows that had none)
- **INC-0103 (orders-db-01, 2025-11-22):**
  - About 222 idle sessions were left open by an orders-api batch Job in namespace `commerce`. The Job opened a new connection for each batch and never closed it.
  - With orders-api's 3 × 25 = 75, that filled the 297 usable slots (300 − 3), so new connections got `53300`.
  - Fix: the Job was stopped, then its idle sessions were ended with `pg_terminate_backend` once team-commerce agreed (RB-012 lever 2).
- **INC-0134 (orders-api-01/03, 2026-02-28, month end):**
  - Month-end order-summary requests each held a `pg` Pool connection for about 30 s while their query ran on orders-db. They filled the 25-connection pools on orders-api-01 and orders-api-03, and `ORD-5003` followed.
  - Fix: team-commerce switched off the order-summary endpoint at 07:58, and the pools drained by 08:05.
  - Not a plan flip; no `ANALYZE`.

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

- Incidents: 117 of 200 written. The step 3 corpus is complete. The demo incident spec and logs are built. Step 4 is complete: 200 labelled questions, split 100/100. Step 5 is complete. Phase 2 (retrieval) is complete: BM25 added, locked config `hybrid` (BM25 + dense, RRF k 60, no rerank) at 256/32 chunks (547), after Prince's override; done-check failed, with cause found (entry above). Phase 3a (LLM wrapper, read-only tools, LangGraph graph, SSE API) is complete; Phase 3a.6 (OpenRouter agent, read_document) and Phase 3b (generation eval on dev, verify on vs off) are complete. Judge validation is done (claim support failed, completeness moderate). Next: Phase 4.
- Phase 0 accounts: Groq, Gemini and Langfuse Cloud keys are in .env and checked by live calls (2026-10-12). AWS, Anthropic credits and GitHub: not checked here.

## Unverified claims (check in Phase 0, record results here)

- AWS: Paid plan vs. Free plan behavior (account closing after 6 months), the $200 credits, the credit-unlock activities.
- Groq: **checked 2026-10-12.** gpt-oss-120b tool use (model page and a live tool call); limits as recorded in the Phase 3a entry.
- Gemini Flash: **checked 2026-10-12.** Limits from Prince's console (Phase 3a entry); model id `gemini-3.5-flash-lite` from Google's model docs; live call OK.
- Langfuse Hobby: **checked 2026-10-12.** 50k units per month (pricing page); a trace arrived (US region).
