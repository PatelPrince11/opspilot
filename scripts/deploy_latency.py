"""Phase 5B: end-to-end latency of the deployed app (docs/deploy.md), measured from the client.

    read -rs OPSPILOT_API_KEY && export OPSPILOT_API_KEY
    uv run python -m scripts.deploy_latency <host> --commit <sha> --app-digest <sha256:...>

20 dev questions (seeded sample), the demo as_of, one at a time over plain HTTP through Nginx.
Wall time per question: perf_counter from just before the POST until the server closes the SSE
stream. Every event's arrival time is kept (streaming check). Status, cost and tool calls come
from GET /investigations/{id}. The server's LLM cache must be off for the run (the override in
docs/deploy.md); cached calls are counted per run so a cached run shows. Writes
eval/results/deploy_latency.json. The key is read from the environment and never printed.
"""

import argparse
import http.client
import json
import os
import random
import sys
import time

from eval.generation_eval import AS_OF, pct
from eval.validate_questions import QUESTIONS, load
from scripts.corpus.common import ROOT, SEED

OUT = ROOT / "eval" / "results" / "deploy_latency.json"


def post(host: str, key: str, question: str) -> dict:
    """One investigation; retries after a 429 (rate limit), recording the waits."""
    body = json.dumps({"question": question, "as_of": AS_OF})
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    waits = []
    while True:
        conn = http.client.HTTPConnection(host, timeout=310)
        t0 = time.perf_counter()
        conn.request("POST", "/investigate", body, headers)
        resp = conn.getresponse()
        if resp.status == 429 and len(waits) < 3:
            wait = int(resp.getheader("Retry-After") or 60) + 1
            waits.append(wait)
            conn.close()
            time.sleep(wait)
            continue
        events, iid = [], resp.getheader("X-Investigation-Id")
        while line := resp.readline():
            if line.startswith(b"event: "):
                events.append([line[7:].strip().decode(), round(time.perf_counter() - t0, 3)])
        wall = time.perf_counter() - t0
        conn.close()
        return {
            "http": resp.status,
            "wall_s": round(wall, 3),
            "events": events,
            "investigation_id": iid,
            "rate_limit_waits_s": waits,
        }


def record(host: str, iid: str | None) -> dict:
    if not iid:
        return {}
    conn = http.client.HTTPConnection(host, timeout=30)
    conn.request("GET", f"/investigations/{iid}")
    inv = json.loads(conn.getresponse().read())
    conn.close()
    calls = inv.get("llm_calls") or []
    return {
        "status": inv.get("status"),
        "cost_usd": inv.get("cost_usd"),
        "tool_calls": len(inv.get("tool_calls") or []),
        "llm_calls": len(calls),
        "cached_llm_calls": sum(bool(c.get("cached")) for c in calls),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--commit", required=True, help="git SHA the running image was built from")
    ap.add_argument("--app-digest", required=True, help="digest of the running app image")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--max-spend", type=float, default=0.25)
    args = ap.parse_args(argv)
    key = os.environ.get("OPSPILOT_API_KEY")
    if not key:
        print("set OPSPILOT_API_KEY (not printed)")
        return 2

    dev = sorted(q["id"] for q in load(QUESTIONS) if q["split"] == "dev")
    ids = random.Random(SEED).sample(dev, args.n)
    text = {q["id"]: q["question"] for q in load(QUESTIONS)}
    runs, spent = [], 0.0
    for i, qid in enumerate(ids, 1):
        if spent >= args.max_spend:
            print(f"stopping: spend ${spent:.4f} reached the cap ${args.max_spend}")
            break
        r = post(args.host, key, text[qid])
        r |= record(args.host, r["investigation_id"])
        spent += r.get("cost_usd") or 0.0
        runs.append({"id": qid, **r})
        print(
            f"{i:2}/{len(ids)} {qid} HTTP {r['http']} {r.get('status')} {r['wall_s']:.1f} s"
            f" cost ${r.get('cost_usd')} tools {r.get('tool_calls')}"
            f" cached {r.get('cached_llm_calls')}/{r.get('llm_calls')}"
        )

    ok = [r for r in runs if r["http"] == 200 and r.get("status") == "done"]
    walls = [r["wall_s"] for r in runs]
    firsts = [r["events"][0][1] for r in runs if r["events"]]
    costs = [r["cost_usd"] for r in runs if r.get("cost_usd") is not None]
    out = {
        "host": args.host,
        "commit": args.commit,
        "app_image_digest": args.app_digest,
        "config": "shipped (agent.run.SHIPPED), server LLM cache off",
        "as_of": AS_OF,
        "seed": SEED,
        "split": "dev",
        "question_ids": ids,
        "n": len(runs),
        "errors": len(runs) - len(ok),
        "cached_llm_calls": sum(r.get("cached_llm_calls") or 0 for r in runs),
        "wall_s": {"p50": pct(walls, 50), "p95": pct(walls, 95), "max": max(walls, default=None)},
        "first_event_s": {"p50": pct(firsts, 50), "p95": pct(firsts, 95)},
        "cost_usd": {
            "n": len(costs),
            "mean": sum(costs) / len(costs) if costs else None,
            "p50": pct(costs, 50),
            "total": round(sum(costs), 6),
        },
        "tool_calls": {"p50": pct([r.get("tool_calls") or 0 for r in runs], 50)},
        "runs": runs,
    }
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    print(
        f"n {out['n']} errors {out['errors']} p50 {out['wall_s']['p50']:.1f} s"
        f" p95 {out['wall_s']['p95']:.1f} s total ${out['cost_usd']['total']}; wrote {OUT}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
