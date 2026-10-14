import hmac
import itertools
import json
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.run import SHIPPED, investigate, load
from config import get_settings
from llm.client import Client

app = FastAPI(title="OpsPilot")
DASHBOARD = (
    Path(__file__).resolve().parents[1] / "frontend" / "dist"
)  # `npm run build` in frontend/; the image builds it


class InvestigateRequest(BaseModel):
    question: str = Field(min_length=5, max_length=2000)
    # The time the question is asked; log windows end here. Defaults to now. The demo
    # incident is at 2026-10-06T14:52:00Z.
    as_of: datetime | None = None


def agent_llm() -> Client:
    return Client.for_role("agent")


def agent_config() -> dict:
    """build_graph overrides for every API investigation: the shipped agent, defenses on."""
    return SHIPPED


class RateLimiter:
    """Sliding-window hit counts per bucket ("key:..." or "ip:..."), in process memory.
    ponytail: one process only; a second worker or replica keeps its own counts, and buckets of
    IPs seen once stay in the dict. Move the counts to Postgres or Redis if the app scales out."""

    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def hit(self, buckets: list[str], limits: list[tuple[int, float]], now: float) -> float:
        """Records one hit in every bucket and returns 0, or, if any bucket is at a limit,
        records nothing and returns the seconds until that bucket has room again."""
        longest = max(w for _, w in limits)
        with self.lock:
            wait = 0.0
            for b in buckets:
                q = self.hits[b]
                while q and q[0] <= now - longest:
                    q.popleft()
                for n, window in limits:
                    inside = [t for t in q if t > now - window]
                    if len(inside) >= n:  # room again once the n-th most recent hit leaves
                        wait = max(wait, inside[-n] + window - now)
            if wait == 0:
                for b in buckets:
                    self.hits[b].append(now)
            return wait


limiter = RateLimiter()


def spent_today() -> float:
    """Recorded LLM spend of investigations started today (UTC). ponytail: a running
    investigation records its cost when it finishes, so concurrent runs can overshoot the
    ceiling by about one investigation's cost each."""
    with psycopg.connect(get_settings().database_url) as conn:
        return conn.execute(
            "SELECT coalesce(sum(cost_usd), 0) FROM investigations"
            " WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
        ).fetchone()[0]


def guard(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> None:
    """POST /investigate only: API key (401), per-key and per-IP rate limits (429), then the
    daily spend ceiling (503). Behind a proxy, run uvicorn with --proxy-headers so
    request.client is the caller, not the proxy."""
    s = get_settings()
    keys = [k.strip() for k in (s.api_keys.get_secret_value() if s.api_keys else "").split(",")]
    given = (x_api_key or "").encode()
    ok = False
    for k in filter(None, keys):  # compare against every key: no early exit, constant time each
        ok |= hmac.compare_digest(k.encode(), given)
    if not ok:
        raise HTTPException(401, "missing or invalid X-API-Key")
    ip = request.client.host if request.client else "unknown"
    wait = limiter.hit(
        [f"key:{x_api_key}", f"ip:{ip}"],
        [(s.rate_per_minute, 60.0), (s.rate_per_day, 86400.0)],
        time.monotonic(),
    )
    if wait:
        raise HTTPException(
            429, "rate limit exceeded", headers={"Retry-After": str(math.ceil(wait))}
        )
    if spent_today() >= s.daily_spend_limit_usd:
        now = datetime.now(UTC)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        raise HTTPException(
            503,
            "daily spend ceiling reached; try again tomorrow (UTC)",
            headers={"Retry-After": str(math.ceil((midnight - now).total_seconds()))},
        )


def sse(events: Iterator[dict]) -> Iterator[str]:
    for e in events:
        data = {k: v for k, v in e.items() if k != "event"}
        yield f"event: {e['event']}\ndata: {json.dumps(data, default=str)}\n\n"


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only: the process is up and serving. DB readiness is the compose healthcheck."""
    return {"status": "ok", "service": "opspilot"}


@app.post("/investigate", dependencies=[Depends(guard)])
def post_investigate(
    req: InvestigateRequest,
    llm: Annotated[Client, Depends(agent_llm)],
    config: Annotated[dict, Depends(agent_config)],
):
    """Server-sent events: start, then tool_call, tool_result, sources, diagnosis, error. The
    investigation id is in the start event and the X-Investigation-Id header, for
    GET /investigations/{id}."""
    iid = uuid.uuid4()
    as_of = (req.as_of or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    start = {"event": "start", "investigation_id": str(iid), "as_of": as_of, "config": config}
    events = itertools.chain(
        [start], investigate(req.question, as_of, llm, investigation_id=iid, config=config)
    )
    return StreamingResponse(
        sse(events),
        media_type="text/event-stream",
        headers={"X-Investigation-Id": str(iid), "Cache-Control": "no-cache"},
    )


@app.get("/investigations/{investigation_id}")
def get_investigation(investigation_id: uuid.UUID) -> dict:
    inv = load(investigation_id)
    if inv is None:
        raise HTTPException(404, "investigation not found")
    return inv


# Last, so the API routes above win. Absent in a backend-only checkout (tests, CI's Python job).
if DASHBOARD.is_dir():
    app.mount("/", StaticFiles(directory=DASHBOARD, html=True), name="dashboard")
