import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.run import SHIPPED, investigate, load
from llm.client import Client

app = FastAPI(title="OpsPilot")


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


def sse(events: Iterator[dict]) -> Iterator[str]:
    for e in events:
        data = {k: v for k, v in e.items() if k != "event"}
        yield f"event: {e['event']}\ndata: {json.dumps(data, default=str)}\n\n"


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only: the process is up and serving. DB readiness is the compose healthcheck."""
    return {"status": "ok", "service": "opspilot"}


@app.post("/investigate")
def post_investigate(
    req: InvestigateRequest,
    llm: Annotated[Client, Depends(agent_llm)],
    config: Annotated[dict, Depends(agent_config)],
):
    """Server-sent events: tool_call, tool_result, sources, diagnosis, error. The investigation
    id is in the X-Investigation-Id header, for GET /investigations/{id}."""
    iid = uuid.uuid4()
    as_of = (req.as_of or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    events = investigate(req.question, as_of, llm, investigation_id=iid, config=config)
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
