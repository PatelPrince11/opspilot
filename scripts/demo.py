"""Run the demo question once and write a transcript: tool calls, evidence, diagnosis, tokens.

    make demo ARGS="--llm ollama:qwen2.5:7b --out scratchpad/phase3/demo_ollama.md"

A smoke test, not a metric: one question, one run. Citation validity is checked here (every
cited id is in the gathered evidence); key facts and forbidden claims from
eval/corpus/demo_answer.toml are listed beside the diagnosis for a human to judge.
"""

import argparse
import json
import time
import tomllib
import uuid
from pathlib import Path

from agent.run import investigate, load
from config import get_settings
from llm.client import Client

ANSWER = Path("eval/corpus/demo_answer.toml")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default=get_settings().dev_llm, help="provider:model")
    ap.add_argument("--as-of", default="2026-10-06T14:52:00Z")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    spec = tomllib.loads(ANSWER.read_text())
    llm = Client.from_spec(args.llm)
    t0 = time.perf_counter()
    events = []
    inv_id = uuid.uuid4()
    for e in investigate(spec["question"], args.as_of, llm, investigation_id=inv_id):
        events.append(e)
        print(e["event"], e.get("name", ""), e.get("message", ""))
    wall = time.perf_counter() - t0
    by = {e["event"]: e for e in events}
    sources = by.get("sources", {}).get("items", [])
    evidence = {s["id"] for s in sources}
    inv = load(inv_id)

    out = [
        f"# Demo smoke: {args.llm}",
        "",
        f"Question: {spec['question']}",
        f"as_of: {args.as_of}",
        f"Investigation: {inv_id} (status {inv['status']})",
        f"Wall time: {wall:.1f} s; verify loops: {inv['loops']}",
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
        " evidence lines (dropped) | ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in inv["llm_calls"]:
        ev = f"{c['evidence_lines']} ({c['evidence_dropped']})" if "evidence_lines" in c else ""
        out.append(
            f"| {c['name']} | {c['prompt_tokens']} | {c['completion_tokens']} |"
            f" {c['estimated']} | {c['prompt_estimate']} | {ev} | {c['latency_ms']:.0f} |"
        )
    out.append(f"| **total** | {inv['prompt_tokens']} | {inv['completion_tokens']} | | | | |")

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
        "## Reference (eval/corpus/demo_answer.toml), judged by hand",
        "",
        "Key facts:",
        *[f"- [ ] {k}" for k in spec["evidence"]["key_facts"]],
        "",
        "Forbidden claims:",
        *[f"- [ ] {k}" for k in spec["evidence"]["forbidden_claims"]],
        "",
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(out))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
