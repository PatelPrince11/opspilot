"""Run one question once and write a transcript: tool calls, evidence, diagnosis, tokens.

    make demo ARGS="--llm ollama:qwen2.5:7b --out scratchpad/phase3/demo_ollama.md"
    make demo ARGS="--llm groq:openai/gpt-oss-120b --qid Q001 --out scratchpad/phase3/q001.md"
    make demo ARGS="--pick 5"        # the seeded, difficulty-stratified dev sample for smoke runs

A smoke test, not a metric: one question, one run. Without --qid it runs the demo question
(eval/corpus/demo_answer.toml); --qid takes dev questions only, never test. Citation validity
(ids and quotes, per verify pass) is deterministic; key facts and forbidden claims are listed
beside the diagnosis for a human to judge. A JSON summary is written next to the transcript.
"""

import argparse
import json
import random
import time
import tomllib
import uuid
from collections import defaultdict
from pathlib import Path

from agent.run import investigate, load
from config import get_settings
from llm.client import Client

ANSWER = Path("eval/corpus/demo_answer.toml")
QUESTIONS = Path("eval/questions.jsonl")


def dev_questions() -> dict[str, dict]:
    qs = [json.loads(line) for line in QUESTIONS.read_text().splitlines() if line.strip()]
    return {q["id"]: q for q in qs if q["split"] == "dev"}


def pick_dev(n: int, seed: int) -> list[str]:
    """n dev ids, allocated to difficulties by largest remainder, then sampled with the seed."""
    by = defaultdict(list)
    for q in dev_questions().values():
        by[q["difficulty"]].append(q["id"])
    total = sum(map(len, by.values()))
    share = {d: n * len(ids) / total for d, ids in sorted(by.items())}
    alloc = {d: int(x) for d, x in share.items()}
    for d in sorted(share, key=lambda d: alloc[d] - share[d])[: n - sum(alloc.values())]:
        alloc[d] += 1
    rng = random.Random(seed)
    return [i for d in sorted(by) for i in sorted(rng.sample(sorted(by[d]), alloc[d]))]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default=get_settings().dev_llm, help="provider:model")
    ap.add_argument("--as-of", default="2026-10-06T14:52:00Z")
    ap.add_argument("--qid", help="a dev question id instead of the demo question")
    ap.add_argument("--pick", type=int, help="print N seeded stratified dev ids and exit")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    if args.pick:
        print(" ".join(pick_dev(args.pick, args.seed)))
        return 0
    if args.out is None:
        ap.error("--out is required")

    if args.qid:
        q = dev_questions().get(args.qid)
        if q is None:
            ap.error(f"{args.qid} is not a dev question (test questions are never run here)")
        question, key_facts, forbidden = q["question"], q["expected_key_facts"], []
        gold = f"{q['difficulty']}; relevant docs {', '.join(q['relevant_doc_ids'])}"
    else:
        spec = tomllib.loads(ANSWER.read_text())
        question = spec["question"]
        key_facts = spec["evidence"]["key_facts"]
        forbidden = spec["evidence"]["forbidden_claims"]
        gold = "demo INC-LIVE-001"
    llm = Client.from_spec(args.llm)
    t0 = time.perf_counter()
    events = []
    inv_id = uuid.uuid4()
    for e in investigate(question, args.as_of, llm, investigation_id=inv_id):
        events.append(e)
        print(e["event"], e.get("name", ""), e.get("message", ""))
    wall = time.perf_counter() - t0
    by = {e["event"]: e for e in events}
    sources = by.get("sources", {}).get("items", [])
    evidence = {s["id"] for s in sources}
    inv = load(inv_id)

    verify_log = by.get("diagnosis", {}).get("verify_log", [])
    waits = sum(c.get("wait_s", 0.0) for c in inv["llm_calls"])
    out = [
        f"# Smoke: {args.qid or 'demo'} on {args.llm}",
        "",
        f"Question: {question}",
        f"Gold: {gold}",
        f"as_of: {args.as_of}",
        f"Investigation: {inv_id} (status {inv['status']})",
        f"Wall time: {wall:.1f} s; verify loops: {inv['loops']}; rate-limit waits: {waits:.1f} s",
        f"Verify passes (claims, bad ids, bad quotes): {verify_log}",
        "",
        "## Tool calls",
        "",
        "| # | tool | arguments | ok | items | output tokens (est.) | ms | note/error |",
        "|---|---|---|---|---|---|---|---|",
    ]
    results = [e for e in events if e["event"] == "tool_result"]
    for i, t in enumerate(results, 1):
        out.append(
            f"| {i} | {t['name']} | `{json.dumps(t['arguments'])}` | {t['success']} |"
            f" {len(t['ids'])} | {t['output_tokens']} | {t['latency_ms']:.0f} |"
            f" {(t['error'] or t['note']).replace('|', '/')} |"
        )
    skipped = [e["message"] for e in events if e["event"] == "error"]
    out += [
        "",
        f"Errors/skips: {skipped or 'none'}",
        "",
        "## LLM calls",
        "",
        "| call | prompt tokens | completion tokens | estimated? | pre-call estimate |"
        " evidence lines (dropped) | ms | rate-limit wait s |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in inv["llm_calls"]:
        ev = f"{c['evidence_lines']} ({c['evidence_dropped']})" if "evidence_lines" in c else ""
        out.append(
            f"| {c['name']} | {c['prompt_tokens']} | {c['completion_tokens']} |"
            f" {c['estimated']} | {c['prompt_estimate']} | {ev} | {c['latency_ms']:.0f} |"
            f" {c.get('wait_s', 0.0):.1f} |"
        )
    out.append(f"| **total** | {inv['prompt_tokens']} | {inv['completion_tokens']} | | | | | |")

    d = by.get("diagnosis", {}).get("diagnosis")
    out += ["", "## Diagnosis", "", "```json", json.dumps(d, indent=1), "```", ""]
    if d:
        cited = [x for c in d["claims"] + d["dropped_claims"] for x in c["evidence_ids"]]
        bad = sorted({x for x in cited if x not in evidence})
        out += [
            "## Citation validity (deterministic)",
            "",
            f"- cited ids: {len(set(cited))}, not in gathered evidence: {bad or 'none'}",
            f"- citations_verified: {d['citations_verified']}",
            "",
            "## Cited evidence",
            "",
        ]
        for s in sources:
            if s["cited"]:
                out.append(f"- `{s['id']}` {s['text']}")
    out += ["", f"## Gathered evidence ({len(sources)} items)", ""]
    out += [f"- `{s['id']}` {s['text'][:160]}" for s in sources]
    out += [
        "",
        "## Reference, judged by hand",
        "",
        "Key facts:",
        *[f"- [ ] {k}" for k in key_facts],
        "",
        "Forbidden claims:",
        *([f"- [ ] {k}" for k in forbidden] or ["- (none listed for dev questions)"]),
        "",
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(out))
    calls = inv["llm_calls"]
    summary = {
        "question": args.qid or "demo",
        "llm": args.llm,
        "status": inv["status"],
        "tool_calls": len(results),
        "tool_calls_ok": sum(t["success"] for t in results),
        "verify_loops": inv["loops"],
        "verify_log": verify_log,
        "llm_calls": len(calls),
        "prompt_tokens": inv["prompt_tokens"],
        "completion_tokens": inv["completion_tokens"],
        "max_call_tokens": max(
            (c["prompt_tokens"] + c["completion_tokens"] for c in calls), default=0
        ),
        "max_call_estimate": max((c["prompt_estimate"] for c in calls), default=0),
        "wall_s": round(wall, 1),
        "rate_limit_wait_s": round(waits, 1),
        "claims": len(d["claims"]) if d else 0,
        "dropped_claims": len(d["dropped_claims"]) if d else 0,
        "confidence": d["confidence"] if d else None,
        "citations_verified": d["citations_verified"] if d else None,
        "key_facts": len(key_facts),
    }
    args.out.with_suffix(".json").write_text(json.dumps(summary, indent=1))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
