"""Live-incident eval (Phase 4 Part B): 10 on-call questions whose answers exist only in logs.

    uv run python -m eval.live_eval load      # put the 10 cases' logs in the DB (idempotent)
    uv run python -m eval.live_eval run       # load, run both configs, score, write

eval/live_questions.jsonl holds the cases and their answer keys (cause, key facts, forbidden
claims), fixed and committed before any run. A case's logs come from data/live/<id>.toml, built
by the generator and checks of the demo's logs (services.toml hosts, ip:port and codes only).
They sit in log_events as corpus 'live', source 'live:<id>', and only a connection that set
opspilot.injection_case to that id sees them (agent/tools), as with the injection payloads. They
are never documents, so no retrieval eval can see them, and no other eval sets a live case.

Configs: the locked defended agent (verify on, FINAL_DEFENSES) with first look off (the locked
config) and on (first look runs only when triage says live). Each case runs at its own as_of.
Scoring is deterministic, no judge. The answer is the summary, likely_cause and kept claims' text.
- key facts: found when any of the fact's exact tokens appears in the answer (case-insensitive,
  not inside a longer word: "kafka-02" is not in "kafka-021"; "http-nio" is in "http-nio-8080").
- forbidden claims: made when the claim's regex matches a sentence of the answer that has no
  negation cue (NEGATION), so "it is not MISCONF" rules MISCONF out rather than claiming it.
  A regex is an approximation either way: every hit is kept with its sentence for audit.
- gold-evidence coverage: gold entries of the case's spec (`gold = true`) with at least one of
  their rows in the gathered evidence / the case's gold entries.
- tool calls: how many, and how many failed.
Post hoc (added after the first run, reported beside the pre-registered numbers, never instead):
the agent model writes U+2011 non-breaking hyphens ("EDGE‑RL01"), which the exact-token match
misses. posthoc_ascii scores the same answers with Unicode hyphens and spaces folded to ASCII.
Resumable (.cache/live/); no run starts once new spend reaches --max-spend.
"""

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import comb

import psycopg

from agent import tools
from agent.defenses import FINAL_DEFENSES
from agent.graph import build_graph, initial_state
from config import get_settings
from eval.generation_eval import append_jsonl, new_spend, read_jsonl
from eval.injection_eval import mcnemar_exact
from eval.retrieval_eval import RESULTS, provenance, write_named
from llm.client import Client, usage
from scripts.corpus.common import DATA, ROOT
from scripts.corpus.services import load_services
from scripts.corpus.synthetic_logs import live_events

CASES = ROOT / "eval" / "live_questions.jsonl"
SPECS = DATA / "live"
PROGRESS = ROOT / ".cache" / "live"
OUT = RESULTS / "live_incidents.json"
# Fixed row ids (base + 1000 x case number + position), so evidence ids, prompts and the
# response cache are the same on every load. Injection payloads use 900,001-900,060.
LIVE_ID_BASE = 950_000
CONFIGS = {
    "defended": FINAL_DEFENSES | {"first_look": False},  # the locked config
    "defended_first_look": FINAL_DEFENSES | {"first_look": True},
}
NEGATION = re.compile(
    r"\b(not|never|neither|nor|isn't|wasn't|aren't|weren't|doesn't|didn't|unrelated|ruled out"
    r"|rather than|instead of)\b|\bno (sign|evidence|indication)\b",
    re.I,
)


def load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]


def case_events(case_id: str, services_doc: dict | None = None) -> list[dict]:
    """The case's log rows with their fixed ids; a row from a gold entry keeps its `gold` key."""
    doc = services_doc or load_services(DATA / "services.toml")
    base = LIVE_ID_BASE + 1000 * int(case_id.split("-")[1])
    return [e | {"id": base + i} for i, e in enumerate(live_events(SPECS / f"{case_id}.toml", doc))]


def load_corpus(conn, cases: list[dict]) -> int:
    """Replace the live corpus with these cases' logs. The caller commits."""
    conn.execute("DELETE FROM log_events WHERE corpus = 'live'")
    doc = load_services(DATA / "services.toml")
    n = 0
    for c in cases:
        for e in case_events(c["id"], doc):
            conn.execute(
                "INSERT INTO log_events (id, ts, service, level, message, source, corpus)"
                " OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s, %s, 'live')",
                (e["id"], e["ts"], e["service"], e["level"], e["message"], f"live:{c['id']}"),
            )
            n += 1
    return n


# ---------------------------------------------------------------------- scoring


ASCII = str.maketrans(
    {c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"}
    | {c: " " for c in "\u00a0\u202f\u2009"}
)


def answer_text(d: dict, ascii_fold: bool = False) -> str:
    text = "\n".join([d["summary"], d["likely_cause"], *(c["text"] for c in d["claims"])])
    return text.translate(ASCII) if ascii_fold else text


def has_token(text: str, token: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(token)}(?!\w)", text, re.I) is not None


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?;])\s+|\n", text) if s.strip()]


def forbidden_hits(text: str, pattern: str) -> list[str]:
    """Sentences that match the claim's regex and carry no negation cue."""
    rx = re.compile(pattern, re.I)
    return [s for s in sentences(text) if rx.search(s) and not NEGATION.search(s)]


def score(case: dict, run: dict, gold: dict[int, str], ascii_fold: bool = False) -> dict:
    """gold: row id -> gold entry, for this case's rows from gold entries."""
    text = answer_text(run["diagnosis"], ascii_fold)
    facts = [any(has_token(text, t) for t in f["any"]) for f in case["key_facts"]]
    hits = {f["claim"]: h for f in case["forbidden"] if (h := forbidden_hits(text, f["pattern"]))}
    seen = {gold[i] for i in run["log_ids"] if i in gold}
    entries = set(gold.values())
    return {
        "key_facts_found": sum(facts),
        "key_facts_total": len(facts),
        "key_facts": facts,
        "all_key_facts": all(facts),
        "forbidden_claims": len(hits),
        "forbidden_hits": hits,
        "gold_covered": len(seen),
        "gold_total": len(entries),
        "tool_calls": len(run["tool_calls"]),
        "tool_failures": sum(not t["success"] for t in run["tool_calls"]),
        "triage_live": run["triage"].get("incident_time") == "live",
    }


def gold_ids(case_id: str) -> dict[int, str]:
    return {e["id"]: e["gold"] for e in case_events(case_id) if "gold" in e}


# ---------------------------------------------------------------------- runs


def run_case(case: dict, config: str, llm: Client | None = None, url=None) -> dict:
    llm = llm or Client.from_spec(get_settings().agent_llm)
    t0 = time.perf_counter()
    with tools.connect(url) as ro:
        ro.execute("SELECT set_config('opspilot.injection_case', %s, false)", [case["id"]])
        graph = build_graph(llm, ro, verify_enabled=True, **CONFIGS[config])
        final = graph.invoke(initial_state(case["question"], case["as_of"]))
    return {
        "id": case["id"],
        "config": config,
        "triage": final["triage"],
        "diagnosis": final["diagnosis"],
        "log_ids": sorted(int(k[4:]) for k in final["evidence"] if k.startswith("log:")),
        "tool_calls": [
            {k: t[k] for k in ("name", "arguments", "success", "error")}
            for t in final["tool_calls"]
        ],
        "llm_calls": final["llm_calls"],
        "wall_s": round(time.perf_counter() - t0, 2),
    }


def run_stage(cases: list[dict], config: str, workers: int, max_spend: float) -> None:
    """max_spend counts new spend over both configs' progress files."""
    path = PROGRESS / f"{config}_runs.jsonl"
    done = read_jsonl(path)
    spent = sum(new_spend(read_jsonl(PROGRESS / f"{c}_runs.jsonl").values()) for c in CONFIGS)
    todo = [c for c in cases if c["id"] not in done]
    print(f"{config}: {len(done)} done, {len(todo)} to run; new spend so far ${spent:.4f}")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {}
        for c in todo:
            if spent >= max_spend:
                print(f"STOPPED: new spend ${spent:.4f} reached --max-spend ${max_spend}")
                break
            futs[pool.submit(run_case, c, config)] = c
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:  # LLM or DB failure: left for the next run
                print(f"  {c['id']} FAILED {type(exc).__name__}: {str(exc)[:200]}")
                continue
            append_jsonl(path, row)
            spent += new_spend([row])
            print(f"  {c['id']} ok: {len(row['tool_calls'])} tool calls, spend ${spent:.4f}")


def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact sign test over the non-tied pairs (ties dropped)."""
    n = wins + losses
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(comb(n, i) for i in range(min(wins, losses) + 1)) / 2**n)


def summarize(rows: list[dict]) -> dict:
    tot = usage([c for r in rows for c in r["llm_calls"]])
    s = [r["scores"] for r in rows]
    return {
        "n": len(rows),
        "key_facts_found": sum(x["key_facts_found"] for x in s),
        "key_facts_total": sum(x["key_facts_total"] for x in s),
        "cases_all_key_facts": sum(x["all_key_facts"] for x in s),
        "cases_with_forbidden_claim": sum(x["forbidden_claims"] > 0 for x in s),
        "forbidden_claims": sum(x["forbidden_claims"] for x in s),
        "gold_covered": sum(x["gold_covered"] for x in s),
        "gold_total": sum(x["gold_total"] for x in s),
        "tool_calls": sum(x["tool_calls"] for x in s),
        "tool_failures": sum(x["tool_failures"] for x in s),
        "triaged_live": sum(x["triage_live"] for x in s),
        "llm_usage": tot,
        "spend_usd": round(new_spend(rows), 6),
    }


def compare(a: list[dict], b: list[dict]) -> dict:
    """b minus a, paired by case: key facts found (sign test) and all key facts (McNemar)."""
    ra, rb = {r["id"]: r["scores"] for r in a}, {r["id"]: r["scores"] for r in b}
    ids = sorted(ra.keys() & rb.keys())
    d = [rb[i]["key_facts_found"] - ra[i]["key_facts_found"] for i in ids]
    wins, losses = sum(x > 0 for x in d), sum(x < 0 for x in d)
    only_a = sum(ra[i]["all_key_facts"] and not rb[i]["all_key_facts"] for i in ids)
    only_b = sum(rb[i]["all_key_facts"] and not ra[i]["all_key_facts"] for i in ids)
    return {
        "n": len(ids),
        "key_facts_better": wins,
        "key_facts_worse": losses,
        "key_facts_tied": len(ids) - wins - losses,
        "key_facts_sign_p": round(sign_test(wins, losses), 4),
        "all_key_facts_only_first": only_a,
        "all_key_facts_only_second": only_b,
        "all_key_facts_mcnemar_p": round(mcnemar_exact(only_a, only_b), 4),
        "gold_covered_diff": sum(rb[i]["gold_covered"] - ra[i]["gold_covered"] for i in ids),
        "tool_calls_diff": sum(rb[i]["tool_calls"] - ra[i]["tool_calls"] for i in ids),
    }


def ascii_scored(cases: list[dict], rows: list[dict]) -> list[dict]:
    return [
        r | {"scores": score(c, r, gold_ids(c["id"]), ascii_fold=True)}
        for c, r in zip(cases, rows, strict=True)
    ]


def write_results(cases: list[dict]) -> dict | None:
    s = get_settings()
    out = {"configs": {}, "cases": [c["id"] for c in cases]}
    rows_by = {}
    for config in CONFIGS:
        runs = read_jsonl(PROGRESS / f"{config}_runs.jsonl")
        if any(c["id"] not in runs for c in cases):
            print(f"{config}: not every case has run; no result file yet")
            return None
        rows = []
        for c in cases:
            r = runs[c["id"]]
            rows.append(r | {"scores": score(c, r, gold_ids(c["id"]))})
        rows_by[config] = rows
        folded = ascii_scored(cases, rows)
        out["configs"][config] = {
            "build_graph": CONFIGS[config],
            "summary": summarize(rows),
            "posthoc_ascii": {
                k: v
                for k, v in summarize(folded).items()
                if k in ("key_facts_found", "cases_all_key_facts", "cases_with_forbidden_claim")
            }
            | {"key_facts": {r["id"]: r["scores"]["key_facts_found"] for r in folded}},
            "per_case": [
                {"id": r["id"], **r["scores"], "likely_cause": r["diagnosis"]["likely_cause"]}
                for r in rows
            ],
        }
    out["comparison"] = compare(rows_by["defended"], rows_by["defended_first_look"])
    out["comparison_posthoc_ascii"] = compare(*(ascii_scored(cases, rows_by[k]) for k in CONFIGS))
    out["models"] = {"agent": s.agent_llm, "guard": s.guard_llm}
    content = json.loads(json.dumps(out))
    wrote = write_named(OUT, content, provenance())
    print(f"eval/results/{OUT.name}: {'written' if wrote else 'unchanged'}")
    return content


def main(argv=None) -> int:
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("load", "run"))
    ap.add_argument("--config", choices=(*CONFIGS, "both"), default="both")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--max-spend", type=float, default=0.3, help="USD of new spend, both configs")
    args = ap.parse_args(argv)
    cases = load_cases()
    with psycopg.connect(s.database_url) as conn:
        print(f"loaded {load_corpus(conn, cases)} live log rows")
    if args.action == "load":
        return 0
    for config in CONFIGS if args.config == "both" else [args.config]:
        run_stage(cases, config, args.concurrency, args.max_spend)
    content = write_results(cases)
    if content:
        for name, c in content["configs"].items():
            print(name, json.dumps(c["summary"]))
        print("comparison", json.dumps(content["comparison"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
