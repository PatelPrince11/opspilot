"""False alarms of the injection defenses D2 (flag) and D3 (Prompt Guard) on clean data (Phase 4).

    uv run python -m eval.false_alarms      # -> eval/results/injection_false_alarms.json

Clean data: every main-corpus chunk (547: runbooks and incident write-ups, scored whole, as
read_document shows them) and a seeded sample of 500 main-corpus log lines, scored as query_logs
prints them ("<ts> <LEVEL> <message>"). The 60 payload carriers (eval/injection.jsonl) are
scored the same way, for the detection rate beside the false-alarm rate. Prompt Guard calls go
through llm/client.py and its response cache, so a rerun is free.
"""

import json
import random
import sys
from collections import defaultdict
from datetime import UTC

import psycopg

from agent.defenses import FLAG_RX, Guard, flag_spans
from config import get_settings
from eval.injection_eval import load_cases
from eval.retrieval_eval import RESULTS
from llm.client import Client
from scripts.corpus.common import SEED

LOG_SAMPLE = 500
TOP = 10


def clean_items(conn) -> list[dict]:
    chunks = conn.execute(
        "SELECT c.document_id || '#' || c.chunk_index, d.doc_type, c.content FROM chunks c"
        " JOIN documents d ON d.id = c.document_id WHERE d.corpus = 'main' ORDER BY c.id"
    ).fetchall()
    logs = conn.execute(
        "SELECT id, ts, level, message FROM log_events WHERE corpus = 'main' ORDER BY id"
    ).fetchall()
    sample = sorted(random.Random(SEED).sample(logs, LOG_SAMPLE))
    return [{"id": i, "kind": t, "text": x} for i, t, x in chunks] + [
        {
            "id": f"log:{i}",
            "kind": "log",
            "text": f"{ts.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ} {lv} {m}",
        }
        for i, ts, lv, m in sample
    ]


def payload_items() -> list[dict]:
    out = []
    for c in load_cases():
        if c["location"] == "log":
            e = c["log"]
            text = f"{e['ts']} {e['level']} {e['message']}"
        else:
            text = c["doc"]["text"]
        out.append({"id": c["id"], "kind": c["location"], "text": text, "style": c["style"]})
    return out


def score(items: list[dict], guard: Guard) -> None:
    for n, it in enumerate(items, 1):
        it["d2"] = bool(flag_spans(it["text"]))
        it["d2_matches"] = sorted({m.group(0)[:60] for m in FLAG_RX.finditer(it["text"])})
        it["d3"] = round(guard.score_one(it["text"]), 4)
        if n % 50 == 0:
            print(f"  {n}/{len(items)}", flush=True)


def rates(items: list[dict], threshold: float, key: str) -> dict:
    by = defaultdict(list)
    for it in items:
        by[it[key]].append(it)
        by["all"].append(it)
    return {
        k: {
            "n": len(v),
            "d2": sum(i["d2"] for i in v),
            "d3": sum(i["d3"] >= threshold for i in v),
            "either": sum(i["d2"] or i["d3"] >= threshold for i in v),
        }
        for k, v in sorted(by.items())
    }


def main() -> int:
    s = get_settings()
    guard = Guard(Client.from_spec(s.guard_llm), s.guard_threshold)
    with psycopg.connect(s.database_url) as conn:
        clean = clean_items(conn)
    payloads = payload_items()
    print(f"scoring {len(clean)} clean items and {len(payloads)} payload carriers")
    score(clean, guard)
    score(payloads, guard)
    t = guard.threshold
    flagged = [i for i in clean if i["d2"] or i["d3"] >= t]
    out = {
        "guard_model": s.guard_llm,
        "guard_threshold": t,
        "log_sample": {"n": LOG_SAMPLE, "seed": SEED},
        "clean": rates(clean, t, "kind"),
        "payloads": rates(payloads, t, "kind"),
        "payloads_by_style": rates(payloads, t, "style"),
        "top_clean_by_guard": [
            {k: i[k] for k in ("id", "kind", "d3", "d2", "d2_matches")} | {"text": i["text"][:200]}
            for i in sorted(clean, key=lambda i: -i["d3"])[:TOP]
        ],
        "clean_flagged": [
            {k: i[k] for k in ("id", "kind", "d3", "d2", "d2_matches")} | {"text": i["text"][:200]}
            for i in flagged
        ],
        "payloads_missed_by_both": [i["id"] for i in payloads if not i["d2"] and i["d3"] < t],
        "guard_calls": len(guard.calls),
        "guard_cost_usd": round(sum(c["cost_usd"] for c in guard.calls if not c["cached"]), 6),
    }
    path = RESULTS / "injection_false_alarms.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps({k: out[k] for k in ("clean", "payloads", "guard_calls", "guard_cost_usd")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
