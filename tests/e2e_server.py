"""The real app (API + built dashboard) with the stub LLM and fake tools, for the Playwright smoke
test (frontend/e2e). Needs Postgres (writes go to a throwaway opspilot_e2e database) and
frontend/dist (`npm run build`). No API keys, no network.

    uv run python -m tests.e2e_server [port]
"""

import os
import sys
import time

import psycopg
import uvicorn
from alembic import command
from alembic.config import Config

from agent import graph as graph_mod
from agent.run import SHIPPED
from api.main import DASHBOARD, agent_config, agent_llm, app
from config import get_settings
from tests.stub_llm import TRIAGE, StubLLM, calls, draft, say
from tests.test_graph import fake_tool
from tests.test_schema import ALEMBIC_INI, with_db

DB = "opspilot_e2e"
DELAY_S = 0.4  # per LLM call, so the stream is visibly incremental


class SlowStub(StubLLM):
    def post(self, body: dict) -> dict:
        time.sleep(DELAY_S)
        return super().post(body)


def script() -> list:
    # First look runs get_service_status, then query_logs (log:1_<i>, "ERROR baz ..." first).
    return [
        TRIAGE,
        calls(("search_runbooks", {"query": "connection refused"})),
        say("done"),
        draft(("payments-api logged connection errors", ["log:1_0"], "ERROR baz xxxxxxxxxx")),
    ]


def fresh_llm() -> StubLLM:
    graph_mod.run_tool = fake_tool()  # ids restart at 0 for each investigation
    return SlowStub(script())


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    if not DASHBOARD.is_dir():
        sys.exit("frontend/dist is missing: run `npm run build` in frontend/ first")
    base = get_settings().database_url
    with psycopg.connect(with_db(base, "postgres"), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {DB}")
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["database_url"] = with_db(base, DB)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    os.environ["DATABASE_URL"] = with_db(base, DB)
    get_settings.cache_clear()
    app.dependency_overrides[agent_llm] = fresh_llm
    # Shipped minus Prompt Guard, which needs Groq.
    app.dependency_overrides[agent_config] = lambda: SHIPPED | {"guard": "off"}
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
