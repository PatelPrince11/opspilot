# OpsPilot

[![CI](https://github.com/PatelPrince11/opspilot/actions/workflows/ci.yml/badge.svg)](https://github.com/PatelPrince11/opspilot/actions/workflows/ci.yml)

An incident-investigation agent: an on-call engineer describes a problem, and the agent searches runbooks, past incidents and logs, then returns a diagnosis where every claim cites its evidence.

**All runbooks, incidents, services and incident logs in this repo are synthetic: they describe a fictional company, not any real organisation or system.** Public LogHub data is used only as background noise.

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

LogHub license notice, verbatim from `LICENSE` at the pinned commit `dd61d09` (sha256 `9ffa6ae2…`):

> LICENSE OF LOGHUB
>
> The datasets are freely available for research or academic work, subject to
> the following condition: For any usage or distribution of the loghub datasets,
> please refer to the loghub repository URL (https://github.com/logpai/loghub)
> and cite the following loghub paper where applicable.
>
> Jieming Zhu, Shilin He, Pinjia He, Jinyang Liu, Michael R. Lyu. Loghub: A Large
> Collection of System Log Datasets for AI-driven Log Analytics. In ISSRE, 2023.
>
> The above license notice shall be included in all copies of the datasets.
