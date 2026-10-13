"""The agent's five read-only tools. Pydantic in, Pydantic out; the database enforces read-only.

    with connect() as conn:                       # role opspilot_ro (migration 0004)
        out = run_tool("query_logs", {"service": "payments-api", "start": ..., "end": ...}, conn)
        out.items[0].id                            # "log:4711"

Every item the agent sees is Evidence with a stable id, the only thing a diagnosis may cite:
- search_runbooks / search_incidents: "RB-027#3" (document id # chunk index), the locked
  retrieval filtered by doc_type, deduped to documents, top 5, snippet capped.
- query_logs: "log:<log_events.id>", one service, a required window of at most 24 h, optional
  minimum level and text match, at most 50 rows in time order.
- get_service_status: "status:<service>".
- read_document: one document's chunks in order, full text, ids as in search ("RB-027#3"), paged
  to about PAGE_TOKENS per call. Search finds documents; this reads them.
Snippets are capped so that a full gather (8 calls) fits the draft's token budget.
No injection defenses yet: Phase 4 measures attack success before adding them.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal

import psycopg
from psycopg.conninfo import make_conninfo
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from config import get_settings
from llm.client import estimate_tokens
from rag.retrieval import RetrievalConfig, retrieve

SNIPPET_CHARS = 400
LOG_CHARS = 240
TOP_DOCS = 5
MAX_LOG_ROWS = 50
MAX_WINDOW = timedelta(hours=24)
LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "FATAL")
PAGE_TOKENS = 1400  # one page of read_document; with its header it fits LATEST_RESULT_TOKENS


class ToolError(ValueError):
    """Invalid arguments: reported back as a failed tool call, never raised to the user."""


class Evidence(BaseModel):
    id: str
    source: Literal["runbook", "incident", "log", "status"]
    text: str
    metadata: dict = {}


class ToolOutput(BaseModel):
    items: list[Evidence]
    note: str = ""


class SearchInput(BaseModel):
    query: str = Field(min_length=3, max_length=300, description="What to look for")


class QueryLogsInput(BaseModel):
    service: str = Field(description="Service name, e.g. payments-api")
    start: datetime = Field(description="Window start, ISO 8601 UTC")
    end: datetime = Field(description="Window end, ISO 8601 UTC; at most 24 h after start")
    level: Literal[LEVELS] | None = Field(None, description="Minimum level, e.g. WARN")
    text: str | None = Field(
        None,
        max_length=100,
        description="Exact substring to match, any case; omit unless you know the exact wording",
    )

    @field_validator("start", "end")
    @classmethod
    def utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)

    @model_validator(mode="after")
    def window(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.end - self.start > MAX_WINDOW:
            raise ValueError("the time range may span at most 24 hours")
        return self


class StatusInput(BaseModel):
    service: str | None = Field(None, description="One service; omit for all services")


class ReadDocumentInput(BaseModel):
    doc_id: str = Field(min_length=3, max_length=40, description="Document id, e.g. INC-0107")
    part: int = Field(1, ge=1, description="Page number, 1 first; the result says how many")

    @field_validator("doc_id")
    @classmethod
    def bare(cls, v: str) -> str:
        return v.split("#")[0].strip()  # a search result id (INC-0107#2) names its document


def connect(url: str | None = None) -> psycopg.Connection:
    """A connection as opspilot_ro: SELECT only, read-only transactions by default."""
    s = get_settings()
    return psycopg.connect(
        make_conninfo(
            url or s.database_url,
            user="opspilot_ro",
            password=s.readonly_db_password.get_secret_value(),
        ),
        autocommit=True,
    )


def search(conn, query: str, doc_type: str) -> ToolOutput:
    cfg = RetrievalConfig(doc_type=doc_type)
    hits = retrieve(query, replace(cfg, k=cfg.candidates), conn)
    best, items = set(), []
    for h in hits:
        if h.document_id in best:
            continue
        best.add(h.document_id)
        meta = {k: h.metadata[k] for k in ("heading_path", "services", "date") if k in h.metadata}
        items.append(
            Evidence(
                id=f"{h.document_id}#{h.chunk_index}",
                source=doc_type,
                text=h.content[:SNIPPET_CHARS],
                metadata=meta | {"score": round(h.score, 4)},
            )
        )
        if len(items) == TOP_DOCS:
            break
    return ToolOutput(items=items)


def search_runbooks(conn, args: SearchInput) -> ToolOutput:
    return search(conn, args.query, "runbook")


def search_incidents(conn, args: SearchInput) -> ToolOutput:
    return search(conn, args.query, "incident")


def query_logs(conn, args: QueryLogsInput) -> ToolOutput:
    sql = (
        "SELECT id, ts, service, level, message, count(*) OVER () FROM log_events"
        " WHERE service = %s AND ts >= %s AND ts <= %s"
    )
    params: list = [args.service, args.start, args.end]
    if args.level:
        sql += " AND level = ANY(%s)"
        params.append(list(LEVELS[LEVELS.index(args.level) :]))
    if args.text:
        sql += r" AND message ILIKE %s ESCAPE '\'"
        esc = args.text.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        params.append(f"%{esc}%")
    rows = conn.execute(sql + " ORDER BY ts, id LIMIT %s", [*params, MAX_LOG_ROWS]).fetchall()
    total = rows[0][5] if rows else 0
    items = [
        Evidence(
            id=f"log:{i}",
            source="log",
            text=f"{ts.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ} {lvl} {msg}"[:LOG_CHARS],
            metadata={"service": svc},
        )
        for i, ts, svc, lvl, msg, _ in rows
    ]
    note = f"{len(items)} of {total} matching rows" + (
        "; narrow the window, level or text to see the rest" if total > len(items) else ""
    )
    return ToolOutput(items=items, note=note)


def get_service_status(conn, args: StatusInput) -> ToolOutput:
    sql = "SELECT name, status, owner, dependencies FROM services"
    rows = conn.execute(
        sql + (" WHERE name = %s" if args.service else "") + " ORDER BY name",
        [args.service] if args.service else [],
    ).fetchall()
    items = [
        Evidence(
            id=f"status:{n}",
            source="status",
            text=f"{n}: {st} (owner {o}; depends on {', '.join(d) or 'nothing'})",
            metadata={"status": st},
        )
        for n, st, o, d in rows
    ]
    note = ""
    if not rows and args.service:
        known = [r[0] for r in conn.execute("SELECT name FROM services ORDER BY name")]
        note = f"unknown service {args.service!r}; known services: {', '.join(known)}"
    return ToolOutput(items=items, note=note)


def pages(chunks: list[tuple]) -> list[list[tuple]]:
    """Chunks in order, cut into pages of at most PAGE_TOKENS (a page holds at least one)."""
    out, used = [[]], 0
    for c in chunks:
        cost = estimate_tokens(c[2])
        if out[-1] and used + cost > PAGE_TOKENS:
            out.append([])
            used = 0
        out[-1].append(c)
        used += cost
    return out


def read_document(conn, args: ReadDocumentInput) -> ToolOutput:
    rows = conn.execute(
        "SELECT d.doc_type, c.chunk_index, c.content, c.metadata FROM documents d"
        " JOIN chunks c ON c.document_id = d.id WHERE d.id = %s ORDER BY c.chunk_index",
        [args.doc_id],
    ).fetchall()
    if not rows:
        raise ToolError(f"no document {args.doc_id!r}; use an id from a search result")
    paged = pages(rows)
    if args.part > len(paged):
        raise ToolError(f"{args.doc_id} has {len(paged)} part(s); part {args.part} does not exist")
    items = [
        Evidence(
            id=f"{args.doc_id}#{i}",
            source=doc_type,
            text=content,
            metadata={k: meta[k] for k in ("heading_path", "date") if k in meta} | {"read": True},
        )
        for doc_type, i, content, meta in paged[args.part - 1]
    ]
    note = f"{args.doc_id} part {args.part} of {len(paged)}" + (
        f"; read_document part {args.part + 1} for the rest" if args.part < len(paged) else ""
    )
    return ToolOutput(items=items, note=note)


TOOLS = {
    "search_runbooks": (
        SearchInput,
        search_runbooks,
        "Search the runbooks (how-to and diagnosis guides). Returns top 5.",
    ),
    "search_incidents": (
        SearchInput,
        search_incidents,
        "Search past incident write-ups. Returns top 5.",
    ),
    "query_logs": (
        QueryLogsInput,
        query_logs,
        "Log lines of one service in a time window (max 24 h), oldest first, max 50.",
    ),
    "get_service_status": (
        StatusInput,
        get_service_status,
        "Current status, owner and dependencies of services.",
    ),
    "read_document": (
        ReadDocumentInput,
        read_document,
        "Read one runbook or incident in full, in order, by id (from a search result). Long"
        " documents come in parts.",
    ),
}


def run_tool(name: str, args: dict, conn) -> ToolOutput:
    if name not in TOOLS:
        raise ToolError(f"unknown tool {name!r}; tools: {', '.join(TOOLS)}")
    model, fn, _ = TOOLS[name]
    try:
        parsed = model.model_validate(args)
    except ValidationError as exc:
        raise ToolError(f"{name}: invalid arguments: {exc.errors(include_url=False)}") from exc
    return fn(conn, parsed)


def tool_schemas() -> list[dict]:
    """OpenAI-format tool definitions, as llm/client.py sends them."""
    out = []
    for name, (model, _, desc) in TOOLS.items():
        schema = model.model_json_schema()
        schema.pop("title", None)
        for p in schema.get("properties", {}).values():
            p.pop("title", None)
        out.append(
            {
                "type": "function",
                "function": {"name": name, "description": desc, "parameters": schema},
            }
        )
    return out
