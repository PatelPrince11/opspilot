# OpsPilot

An incident-investigation agent: an on-call engineer describes a problem, and the agent searches runbooks, past incidents and logs, then returns a diagnosis where every claim cites its evidence.

**All runbooks, incidents and incident logs in this repo are synthetic.** Public LogHub data is used only as background noise.

Work in progress. See `docs/BUILD_GUIDE.md` for the plan and `NOTES.md` for decisions.

## Quick start

```bash
cp .env.example .env
make up      # Postgres + pgvector, migrations (one-shot container), then the FastAPI app
curl 127.0.0.1:8001/health
make test    # schema tests need the stack up; they skip with a reason otherwise
```

Migrations are Alembic, in `db/migrations/`. `make migrate` / `make migrate-down` run them from the host (via `localhost:5433`); `make psql` opens a shell in the database.

## Data

`make corpus` builds the corpus files in the app container from the hand-written sources in `data/` (services, runbooks, incidents) and writes `data/processed/` plus a `MANIFEST.json` of hashes. `make corpus-check` verifies the committed outputs are up to date without writing anything. Everything except LogHub is synthetic.

**LogHub (background noise only).** The OpenStack sample from [LogHub](https://github.com/logpai/loghub) is downloaded at build time, pinned to a commit and checked by sha256. It is not committed: its license makes the datasets "freely available for research or academic work" and requires the license notice in every copy (saved next to the download in `data/raw/loghub/LICENSE`). Citation:

> Jieming Zhu, Shilin He, Pinjia He, Jinyang Liu, Michael R. Lyu. Loghub: A Large Collection of System Log Datasets for AI-driven Log Analytics. In ISSRE, 2023.
