"""One investigation: persist it, stream the graph's events, persist every tool call.

    for event in investigate(question, "2026-10-06T14:52:00Z", Client.for_role("agent")):
        event["event"]   # tool_call, tool_result, sources, diagnosis, error

Writes go through the app's own role; the tools read through opspilot_ro (agent/tools).
"""

import json
import uuid
from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row

from agent import tools
from agent.defenses import FINAL_DEFENSES
from agent.graph import build_graph, initial_state
from config import get_settings
from llm.client import Client, usage

# The agent the API runs: the locked dev config (verify on) plus the frozen injection defenses
# and the live-only first look (Prince's call after the live-incident set; NOTES, Phase 4).
# Eval scripts name their configs explicitly; this one is "shipped" in eval/generation_eval.py.
SHIPPED = FINAL_DEFENSES | {"first_look": True}


def investigate(
    question: str,
    as_of: str,
    llm: Client,
    *,
    investigation_id: uuid.UUID | None = None,
    db_url: str | None = None,
    verify_enabled: bool | None = None,
    config: dict | None = None,
) -> Iterator[dict]:
    """config: build_graph overrides (e.g. SHIPPED); None takes the settings."""
    iid = investigation_id or uuid.uuid4()
    verify_enabled = get_settings().verify_enabled if verify_enabled is None else verify_enabled
    url = db_url or get_settings().database_url
    with psycopg.connect(url, autocommit=True) as rw, tools.connect(url) as ro:
        rw.execute(
            "INSERT INTO investigations (id, question, as_of, llm, verify_enabled)"
            " VALUES (%s, %s, %s, %s, %s)",
            (iid, question, as_of, f"{llm.provider}:{llm.model}", verify_enabled),
        )
        final: dict = {}
        try:
            graph = build_graph(llm, ro, verify_enabled=verify_enabled, **(config or {}))
            state = initial_state(question, as_of, trace_id=iid.hex)
            for mode, ev in graph.stream(state, stream_mode=["custom", "values"]):
                if mode == "values":
                    final = ev
                    continue
                if ev["event"] == "tool_result":
                    rw.execute(
                        "INSERT INTO tool_calls (investigation_id, tool, input, output_bytes,"
                        " output_tokens, latency_ms, success, error)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            iid,
                            ev["name"],
                            json.dumps(ev["arguments"], default=str),
                            len(json.dumps(ev["ids"])),
                            ev["output_tokens"],
                            ev["latency_ms"],
                            ev["success"],
                            ev["error"],
                        ),
                    )
                yield ev
        except Exception as exc:
            calls = final.get("llm_calls", [])
            rw.execute(
                "UPDATE investigations SET status = 'error', error = %s, llm_calls = %s,"
                " cost_usd = %s, finished_at = now() WHERE id = %s",
                (
                    f"{type(exc).__name__}: {exc}"[:2000],
                    json.dumps(calls),
                    usage(calls)["cost_usd"],
                    iid,
                ),
            )
            yield {"event": "error", "message": f"{type(exc).__name__}: {exc}"[:500]}
            return
        finally:
            llm.flush()
        calls = final.get("llm_calls", [])
        u = usage(calls)
        llm.trace_usage(iid.hex, u)
        llm.flush()
        rw.execute(
            "UPDATE investigations SET status = 'done', diagnosis = %s, llm_calls = %s,"
            " loops = %s, prompt_tokens = %s, completion_tokens = %s, cost_usd = %s,"
            " finished_at = now() WHERE id = %s",
            (
                json.dumps(final.get("diagnosis")),
                json.dumps(calls),
                final.get("loops"),
                u["prompt_tokens"],
                u["completion_tokens"],
                u["cost_usd"],
                iid,
            ),
        )


def load(investigation_id: uuid.UUID, db_url: str | None = None) -> dict | None:
    with psycopg.connect(db_url or get_settings().database_url) as conn:
        conn.row_factory = dict_row
        row = conn.execute("SELECT * FROM investigations WHERE id = %s", (investigation_id,))
        inv = row.fetchone()
        if inv is None:
            return None
        inv["tool_calls"] = conn.execute(
            "SELECT tool, input, output_tokens, latency_ms, success, error, created_at"
            " FROM tool_calls WHERE investigation_id = %s ORDER BY id",
            (investigation_id,),
        ).fetchall()
        return inv
