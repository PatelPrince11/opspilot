# OpsPilot

An incident-investigation agent: an on-call engineer describes a problem, and the agent searches runbooks, past incidents and logs, then returns a diagnosis where every claim cites its evidence.

**All runbooks, incidents and incident logs in this repo are synthetic.** Public LogHub data is used only as background noise.

Work in progress. See `docs/BUILD_GUIDE.md` for the plan and `NOTES.md` for decisions.

## Quick start

```bash
cp .env.example .env
make up      # Postgres + pgvector and the FastAPI app
curl 127.0.0.1:8001/health
make test
```
