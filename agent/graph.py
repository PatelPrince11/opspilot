"""The investigation graph: triage -> gather (loop) -> draft -> verify -> (gather | finish).

    graph = build_graph(Client.for_role("agent"), tools_conn)
    final = graph.invoke(initial_state("The payment API ...", "2026-10-06T14:52:00Z"))
    final["diagnosis"]                               # a Diagnosis as a dict

- triage (LLM, JSON): services involved, symptoms, log window ending at as_of.
- gather (LLM with tools, a loop): each turn the model calls a tool, sees the result and chooses
  the next call, until it replies without a tool call (done) or the run hits
  AGENT_MAX_TOOL_CALLS. The full evidence stays in state; each turn's prompt holds a compact
  index (id plus one line per item) and only the latest result in full (capped), so every turn
  fits the token budget however much was gathered. After a failed verify the loop resumes with
  the unsupported claims in the prompt.
- draft (LLM, JSON): a Diagnosis from trimmed evidence (ids + capped snippets, near-identical log
  lines collapsed), packed to fit the per-call token budget. Invalid JSON gets one retry.
- verify (no LLM): every claim must cite at least one id, every cited id must be in the
  evidence map, and (VERIFY_QUOTES, on by default) the claim's quote, normalized, must be a
  substring of one cited item's text and at least MIN_QUOTE_WORDS long. On failure it loops
  back to gather at most MAX_LOOPS times; after that, claims with invalid citations or quotes
  are dropped, confidence goes down one level and the diagnosis is marked unverified.
  VERIFY_ENABLED=false routes draft straight to finish.
Events (tool_call, tool_result, sources, diagnosis, error) go to LangGraph's custom stream.
"""

import json
import operator
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Annotated, Literal, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError

from agent.tools import run_tool, tool_schemas
from config import get_settings
from llm.client import Client, LLMError, estimate_tokens

MAX_LOOPS = 2
DRAFT_MAX_TOKENS = 2500  # gpt-oss reasons first: 1,500 cut two of five dev drafts off
TRIAGE_MAX_TOKENS = 1000  # gpt-oss reasons first: 465 tokens on a dated question
GATHER_MAX_TOKENS = 1000  # reasoning models (gpt-oss) spend part of this thinking
LATEST_RESULT_TOKENS = 1500
DRAFT_SNIPPET_CHARS = 300
READ_CHARS = 2000  # a chunk from read_document goes to the draft whole (256 tokens is ~1,100 chars)
INDEX_CHARS = 100  # per line of the compact evidence index gather sees
NUDGE = (
    "You replied without a tool call. Make the call now, as a tool call; reply done only when "
    "finished."
)
MIN_QUOTE_WORDS = 3  # an empty or one-word quote is a substring of almost anything
LOWER = {"high": "medium", "medium": "low", "low": "low"}


# ---------------------------------------------------------------------- output models


class Triage(BaseModel):
    services: list[str] = Field(description="Service names involved, from the known list")
    symptoms: str = Field(description="The reported symptoms in a few words")
    window_start: datetime = Field(description="Start of the log window to inspect, ISO 8601 UTC")
    window_end: datetime = Field(
        description="End of the log window, ISO 8601 UTC, <= as_of, <= 24 h after the start"
    )


class Claim(BaseModel):
    text: str
    evidence_ids: list[str] = Field(description="Ids of gathered evidence supporting the claim")
    quote: str = Field(description="At most 20 words copied exactly from one cited item")


class Draft(BaseModel):
    summary: str
    likely_cause: str
    confidence: Literal["low", "medium", "high"]
    claims: list[Claim]
    next_checks: list[str]


class Diagnosis(Draft):
    """Draft plus what verify decided. citations_verified is None when verify is disabled."""

    citations_verified: bool | None = None
    dropped_claims: list[Claim] = []
    note: str = ""


def merge(a: dict, b: dict) -> dict:
    return {**a, **b}


class State(TypedDict, total=False):
    question: str
    as_of: str
    trace_id: str | None
    started: float
    triage: dict
    evidence: Annotated[dict, merge]  # evidence id -> Evidence dict
    tool_calls: Annotated[list, operator.add]
    llm_calls: Annotated[list, operator.add]
    draft: dict | None
    verify: dict | None
    verify_log: Annotated[list, operator.add]  # one row per verify pass
    loops: int
    diagnosis: dict


def initial_state(question: str, as_of: str, trace_id: str | None = None) -> State:
    return State(
        question=question,
        as_of=as_of,
        trace_id=trace_id,
        started=time.time(),
        evidence={},
        tool_calls=[],
        llm_calls=[],
        draft=None,
        verify=None,
        verify_log=[],
        loops=0,
    )


# ---------------------------------------------------------------------- prompts

SYSTEM = (
    "You are OpsPilot, an incident investigator for an on-call engineer. You can only read: "
    "runbooks, past incident write-ups, service logs and service status. The current time "
    "(as_of) is {as_of}; there are no logs after it. Questions may ask about a past date: then "
    "look at that time, not at as_of."
)

GATHER_INSTRUCTIONS = (
    "Investigate step by step: each turn, call a tool (or a few independent ones), read the "
    "result, then choose the next call. Search runbooks and past incidents for the symptoms, "
    "check service status, and query the logs of the affected service and of the services it "
    "depends on. Each log query needs an explicit start and end, ISO 8601 UTC, at most 24 hours "
    "apart: when the question names a past date or time, set the window around it (if the "
    "question gives no time of day, find the time in the incident write-ups first); otherwise "
    "use a window ending at as_of. Use exact service names from the known list. Do not repeat "
    "a call. Searches find documents but show only the best-matching part of each: search to "
    "find the relevant runbooks and incidents, then read_document to get their details (cause, "
    "timeline, fix). The evidence index shows older results cut to one short line, but every "
    "item is kept in full for the diagnosis: do not read a document again. "
    "When the evidence explains the cause, or nothing more can be found, reply with the word "
    "done and no tool call."
)

DRAFT_INSTRUCTIONS = (
    "Write the diagnosis as JSON with keys summary, likely_cause, confidence (low, medium or "
    "high), claims (each {text, evidence_ids, quote}) and next_checks. Every claim must cite one "
    "or more evidence ids exactly as written in square brackets below. Cite only ids listed "
    "below. quote is 5 to 20 words copied character for character from the text of one cited "
    "item, the words that support the claim. Do not claim what the evidence does not show; say "
    "what is unknown in next_checks."
)


def json_content(text: str) -> str:
    """The JSON object in a reply: fences and prose around it are dropped."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def structured(llm: Client, messages, model, name, max_tokens, state, calls) -> BaseModel | None:
    """One call for a JSON object of `model`, one retry with the validation error. Groq rejects
    output that breaks the schema (often cut off at max_tokens) with a 400 json_validate_failed
    and no usage counts; that is treated as invalid output, so its tokens go unrecorded."""
    schema = model.model_json_schema()
    for attempt in range(2):
        try:
            r = llm.chat(
                messages,
                json_schema=schema,
                max_tokens=max_tokens,
                name=name if attempt == 0 else f"{name}_retry",
                trace_id=state["trace_id"],
            )
        except LLMError as exc:
            if "json_validate_failed" not in str(exc):
                raise
            r, errs = None, "the provider rejected it as invalid JSON; keep it short"
        if r is not None:
            calls.append(r.log_row())
            try:
                return model.model_validate_json(json_content(r.content))
            except ValidationError as exc:
                errs = "; ".join(
                    f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5]
                )
        if attempt == 0:
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": f"Your reply was not valid ({errs}). Reply with the JSON "
                    "object only.",
                },
            ]
    return None


# ---------------------------------------------------------------------- evidence for the draft


def log_key(e: dict) -> tuple:
    """Log lines that differ only in numbers or hex ids (timestamps, hosts, request ids)."""
    msg = e["text"].split(" ", 1)[1]  # drop the timestamp
    return (e["metadata"].get("service"), re.sub(r"\b[0-9a-f]{8}\b|\d+", "#", msg))


def evidence_lines(
    evidence: dict, chars: int = DRAFT_SNIPPET_CHARS, read_chars: int | None = None
) -> list[str]:
    """One line per item, status then logs (near-duplicates collapsed) then runbooks then
    incidents, each text cut to `chars`. With read_chars, chunks from read_document come before
    the search snippets, cut to read_chars instead. The collapsed lines' other ids stay valid
    citations in the evidence map."""
    by_source = defaultdict(list)
    for e in evidence.values():
        by_source[e["source"]].append(e)
    lines = [f"[{e['id']}] {e['text']}" for e in by_source["status"]]
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in sorted(by_source["log"], key=lambda e: (e["text"], e["id"])):
        groups[log_key(e)].append(e)
    for g in sorted(groups.values(), key=lambda g: g[0]["text"]):
        more = f" (and {len(g) - 1} similar lines until {g[-1]['text'][:20]})" if len(g) > 1 else ""
        lines.append(f"[{g[0]['id']}] {g[0]['text'][:chars]}{more}")
    docs = [e for src in ("runbook", "incident") for e in by_source[src]]
    if read_chars is not None:
        read = [e for e in docs if e["metadata"].get("read")]
        lines += [f"[{e['id']}] {' '.join(e['text'].split())[:read_chars]}" for e in read]
        docs = [e for e in docs if not e["metadata"].get("read")]
    lines += [f"[{e['id']}] {' '.join(e['text'].split())[:chars]}" for e in docs]
    return lines


def normalize(text: str) -> str:
    """Case, whitespace, curly quotes and the quote marks or ellipses around a quote ignored."""
    text = text.translate(str.maketrans("\u201c\u201d\u2018\u2019", "\"\"''"))
    return " ".join(text.casefold().split()).strip(" \"'.\u2026")


def quote_found(quote: str, ids: list[str], evidence: dict) -> bool:
    q = normalize(quote)
    return len(q.split()) >= MIN_QUOTE_WORDS and any(
        q in normalize(evidence[i]["text"]) for i in ids if i in evidence
    )


def pack(lines: list[str], allowance: int) -> tuple[list[str], int]:
    """Lines in order until the token allowance is spent; returns (kept, dropped count)."""
    kept, used = [], 0
    for line in lines:
        cost = estimate_tokens(line) + 1
        if used + cost > allowance:
            break
        kept.append(line)
        used += cost
    return kept, len(lines) - len(kept)


# ---------------------------------------------------------------------- graph


def build_graph(
    llm: Client,
    conn,
    *,
    verify_enabled: bool | None = None,
    verify_quotes: bool | None = None,
    max_tool_calls: int | None = None,
    max_seconds: float | None = None,
):
    s = get_settings()
    verify_enabled = s.verify_enabled if verify_enabled is None else verify_enabled
    verify_quotes = s.verify_quotes if verify_quotes is None else verify_quotes
    max_tool_calls = s.agent_max_tool_calls if max_tool_calls is None else max_tool_calls
    max_seconds = s.agent_max_seconds if max_seconds is None else max_seconds
    tools = tool_schemas()

    def out_of_time(state) -> bool:
        return time.time() - state["started"] > max_seconds

    def context(state) -> str:
        return f"as_of: {state['as_of']}\nQuestion: {state['question']}"

    def system(state, instructions: str = "") -> dict:
        text = SYSTEM.format(as_of=state["as_of"])
        return {"role": "system", "content": f"{text} {instructions}".strip()}

    def triage(state: State) -> dict:
        known = ", ".join(r[0] for r in conn.execute("SELECT name FROM services ORDER BY name"))
        calls: list = []
        msgs = [
            system(state),
            {
                "role": "user",
                "content": f"{context(state)}\nKnown services: {known}\n"
                "Reply with JSON: services involved, symptoms, and a log window: about one hour "
                "around the date and time the question names, or ending at as_of if it names "
                "none (the whole named day, at most 24 h, if it gives no time of day).",
            },
        ]
        t = structured(llm, msgs, Triage, "triage", TRIAGE_MAX_TOKENS, state, calls)
        return {"triage": t.model_dump(mode="json") if t else {}, "llm_calls": calls}

    def call_line(i: int, row: dict) -> str:
        got = f"{len(row['ids'])} items" if row["success"] else f"failed: {row['error'][:150]}"
        got += f" ({row['note'][:150]})" if row["success"] and row["note"] else ""
        return f"{i}. {row['name']} {json.dumps(row['arguments'], default=str)} -> {got}"

    def gather_messages(state: State, made: list, evidence: dict, latest: list) -> list[dict]:
        """One turn's prompt: calls so far, a compact index of older evidence and the latest
        turn's results in full, both packed to fit the budget (latest first)."""
        feedback = ""
        if state["verify"] and not state["verify"]["ok"]:
            feedback = (
                "\nThe draft diagnosis had claims the evidence did not support: "
                f"{json.dumps(state['verify']['invalid'])}\nFind evidence for them, if any can "
                "exist, then reply done."
            )
        so_far = "\n".join(call_line(i, r) for i, r in enumerate(made, 1)) or "none"
        head = (
            f"{context(state)}\nTriage: {json.dumps(state['triage'])}{feedback}\n"
            f"Tool calls so far ({len(made)} of {max_tool_calls}):\n{so_far}\n"
        )
        msgs = [
            system(state, GATHER_INSTRUCTIONS),
            {"role": "user", "content": head},
        ]
        room = llm.budget - GATHER_MAX_TOKENS - estimate_tokens(msgs, tools) - 60
        new_ids = {i for r in latest for i in r["ids"]}
        latest_text, room_left = [], room
        for r in latest:
            items = {i: evidence[i] for i in r["ids"]}
            lines = [f"{r['name']} -> {r['note'] or r['error'] or 'ok'}"]
            lines += evidence_lines(items, chars=10_000)
            kept, dropped = pack(lines, min(LATEST_RESULT_TOKENS, room_left))
            latest_text += kept + ([f"({dropped} more lines not shown)"] if dropped else [])
            room_left -= estimate_tokens("\n".join(kept)) + 10
        older = {k: v for k, v in evidence.items() if k not in new_ids}
        index, dropped = pack(evidence_lines(older, INDEX_CHARS), max(room_left, 0))
        if dropped:
            index.append(f"({dropped} more items not shown)")
        msgs[1]["content"] = (
            f"{head}Evidence index (older results, one line each):\n"
            f"{chr(10).join(index) or '(none)'}\n"
            f"Latest result:\n{chr(10).join(latest_text) or '(none yet)'}"
        )
        return msgs

    def run_one(call, write) -> tuple[dict, dict]:
        write({"event": "tool_call", "name": call.name, "arguments": call.arguments})
        t0 = time.perf_counter()
        row = {
            "name": call.name,
            "arguments": call.arguments,
            "success": True,
            "error": None,
            "ids": [],
            "note": "",
            "output_tokens": 0,
        }
        items = {}
        try:
            out = run_tool(call.name, call.arguments, conn)
            items = {e.id: e.model_dump() for e in out.items}
            row |= {"ids": list(items), "note": out.note, "output_tokens": estimate_tokens(items)}
        except Exception as exc:  # ToolError or a DB error: reported, never fatal
            row |= {"success": False, "error": str(exc)[:500]}
        row["latency_ms"] = (time.perf_counter() - t0) * 1000
        write({"event": "tool_result", **row})
        return row, items

    def duplicate(call, first: int, write) -> dict:
        write({"event": "tool_call", "name": call.name, "arguments": call.arguments})
        err = f"duplicate of call {first}, not run: its results are already in the evidence"
        row = {
            "name": call.name,
            "arguments": call.arguments,
            "success": False,
            "error": err,
            "ids": [],
            "note": "",
            "output_tokens": 0,
            "latency_ms": 0.0,
        }
        write({"event": "tool_result", **row})
        return row

    def gather(state: State) -> dict:
        write = get_stream_writer()
        llm_calls, done, new = [], [], {}
        latest: list = []
        nudged = False
        while True:
            made = state["tool_calls"] + done
            if len(made) >= max_tool_calls or out_of_time(state):
                break
            msgs = gather_messages(state, made, state["evidence"] | new, latest)
            if nudged and not made:  # only the turn right after the prose reply
                msgs[1]["content"] += f"\n{NUDGE}"
            r = llm.chat(
                msgs, tools, max_tokens=GATHER_MAX_TOKENS, name="gather", trace_id=state["trace_id"]
            )
            llm_calls.append(r.log_row())
            if not r.tool_calls:
                # Done, unless nothing was called yet and the reply does not say done: gpt-oss
                # sometimes describes the call in prose instead of making it. One nudge.
                if made or nudged or "done" in r.content.lower():
                    break
                nudged = True
                continue
            latest = []
            for call in r.tool_calls:
                if len(state["tool_calls"]) + len(done) >= max_tool_calls:
                    write({"event": "error", "message": f"skipped {call.name}: tool-call cap"})
                    continue
                prior = state["tool_calls"] + done
                same = [
                    i
                    for i, m in enumerate(prior, 1)
                    if (m["name"], m["arguments"]) == (call.name, call.arguments)
                ]
                if same:  # not run again: counted, and the model is told why
                    row, items = duplicate(call, same[0], write), {}
                else:
                    row, items = run_one(call, write)
                new |= items
                done.append(row)
                latest.append(row)
        return {"evidence": new, "tool_calls": done, "llm_calls": llm_calls}

    def draft(state: State) -> dict:
        calls: list = []
        feedback = ""
        if state["verify"] and not state["verify"]["ok"]:
            feedback = (
                "\nYour previous draft had claims that failed the check (bad_ids: cited ids "
                "not in the evidence; quote_found false: the quote is not in a cited item): "
                f"{json.dumps(state['verify']['invalid'])}. Cite only listed ids, copy quotes "
                "exactly, or drop the claim."
            )
        head = f"{context(state)}{feedback}\nEvidence:\n"
        base = [
            system(state, DRAFT_INSTRUCTIONS),
            {"role": "user", "content": head},
        ]
        fixed = estimate_tokens(base, Draft.model_json_schema())
        # Room for one validation retry message (about 80 tokens) is kept free.
        allowance = llm.budget - DRAFT_MAX_TOKENS - fixed - 80
        lines, dropped = pack(evidence_lines(state["evidence"], read_chars=READ_CHARS), allowance)
        base[1]["content"] = head + ("\n".join(lines) or "(none gathered)")
        d = structured(llm, base, Draft, "draft", DRAFT_MAX_TOKENS, state, calls)
        if calls:
            calls[0]["evidence_lines"], calls[0]["evidence_dropped"] = len(lines), dropped
        if d is None:
            get_stream_writer()(
                {
                    "event": "error",
                    "message": "the model returned no valid diagnosis after one retry",
                }
            )
            d = Draft(
                summary="No valid diagnosis was produced.",
                likely_cause="unknown",
                confidence="low",
                claims=[],
                next_checks=[],
            )
        return {"draft": d.model_dump(), "llm_calls": calls}

    def verify(state: State) -> dict:
        ev, invalid, bad_id_n, bad_quote_n = state["evidence"], [], 0, 0
        claims = state["draft"]["claims"]
        for i, c in enumerate(claims):
            bad = [x for x in c["evidence_ids"] if x not in ev] or (
                [] if c["evidence_ids"] else ["(none cited)"]
            )
            found = quote_found(c["quote"], c["evidence_ids"], ev) if verify_quotes else True
            bad_id_n += bool(bad)
            bad_quote_n += not found
            if bad or not found:
                row = {"claim": i, "text": c["text"], "bad_ids": bad}
                if verify_quotes:
                    row |= {"quote": c["quote"], "quote_found": found}
                invalid.append(row)
        retry = bool(invalid) and state["loops"] < MAX_LOOPS and not out_of_time(state)
        log = {"claims": len(claims), "bad_ids": bad_id_n, "bad_quotes": bad_quote_n}
        return {
            "verify": {"ok": not invalid, "invalid": invalid, "retry": retry},
            "verify_log": [log],
            "loops": state["loops"] + retry,
        }

    def after_verify(state: State) -> str:
        return "gather" if state["verify"]["retry"] else "finish"

    def finish(state: State) -> dict:
        write = get_stream_writer()
        d = Diagnosis(**state["draft"])
        v = state["verify"]
        if v is not None:
            d.citations_verified = v["ok"]
            if not v["ok"]:
                bad = {x["claim"] for x in v["invalid"]}
                d.dropped_claims = [c for i, c in enumerate(d.claims) if i in bad]
                d.claims = [c for i, c in enumerate(d.claims) if i not in bad]
                d.confidence = LOWER[d.confidence]
                d.note = (
                    f"{len(bad)} claim(s) dropped after {state['loops']} verify loop(s): "
                    "they cited evidence that was not gathered, or quoted words not in it. "
                    "Confidence lowered."
                )
        cited = {x for c in d.claims for x in c.evidence_ids}
        write(
            {
                "event": "sources",
                "items": [
                    {
                        "id": e["id"],
                        "source": e["source"],
                        "text": e["text"][:200],
                        "metadata": e["metadata"],
                        "cited": e["id"] in cited,
                    }
                    for e in state["evidence"].values()
                ],
            }
        )
        diagnosis = d.model_dump()
        write({"event": "diagnosis", "diagnosis": diagnosis, "verify_log": state["verify_log"]})
        return {"diagnosis": diagnosis}

    g = StateGraph(State)
    for name, fn in [
        ("triage", triage),
        ("gather", gather),
        ("draft", draft),
        ("verify", verify),
        ("finish", finish),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "triage")
    g.add_edge("triage", "gather")
    g.add_edge("gather", "draft")
    if verify_enabled:
        g.add_edge("draft", "verify")
        g.add_conditional_edges("verify", after_verify, ["gather", "finish"])
    else:
        g.add_edge("draft", "finish")
    g.add_edge("finish", END)
    return g.compile()
