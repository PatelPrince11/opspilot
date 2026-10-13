"""Prompt-injection eval (BUILD_GUIDE Phase 4): attack success of the locked agent, per case.

    uv run python -m eval.injection_eval load       # put the payloads in the DB (idempotent)

eval/injection.jsonl holds 30 cases, fixed before any agent run: an injected instruction in a
log message, runbook text or incident notes, aimed at one dev question. A case's check (a
case-insensitive regex) is either its canary string or the specific claim it pushes.

The payloads live in the normal tables, flagged corpus = 'injection' and tagged with their case:
- log cases: one log_events row, source 'injection:<case id>', inside the exact service, window,
  level and text filter the locked agent queries for that question (taken from its dev run).
- runbook and incident cases: one single-chunk document (doc_type runbook or incident) holding
  a short excerpt of a gold document of the question with the payload next to it. It copies
  the gold document's metadata, so the same filters find it, and is tagged in document and
  chunk metadata.
Nothing sees a payload unless its connection set opspilot.injection_case to that case
(rag/retrieval.py, agent/tools); BM25's corpus stats skip the injection corpus (migration 0006).
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import psycopg

from config import get_settings
from rag import embeddings
from scripts.corpus.common import ROOT

CASES = ROOT / "eval" / "injection.jsonl"
LOCATIONS = ("log", "runbook", "incident")
STYLES = ("direct", "fake_system", "oncall_note", "json", "tool_call")


def load_cases(path: Path = CASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_corpus(conn, cases: list[dict]) -> None:
    """Replace the injection corpus with these cases' payloads. The caller commits."""
    conn.execute("DELETE FROM documents WHERE corpus = 'injection'")  # chunks cascade
    conn.execute("DELETE FROM log_events WHERE corpus = 'injection'")
    docs = [c for c in cases if c["location"] != "log"]
    vectors = embeddings.embed([c["doc"]["text"] for c in docs]) if docs else []
    for c, vec in zip(docs, vectors, strict=True):
        d = c["doc"]
        anchor = conn.execute("SELECT metadata FROM documents WHERE id = %s", [d["anchor"]])
        meta = anchor.fetchone()[0] | {"injection_case": c["id"]}
        conn.execute(
            "INSERT INTO documents (id, doc_type, title, content_hash, corpus, metadata)"
            " VALUES (%s, %s, %s, %s, 'injection', %s)",
            (
                d["id"],
                d["doc_type"],
                d["title"],
                hashlib.sha256(d["text"].encode()).hexdigest(),
                json.dumps(meta),
            ),
        )
        conn.execute(
            "INSERT INTO chunks (document_id, chunk_index, content, token_count, embedding,"
            " metadata) VALUES (%s, 0, %s, %s, %s::vector, %s)",
            (
                d["id"],
                d["text"],
                embeddings.count_tokens([d["text"]])[0],
                embeddings.as_pgvector(vec),
                json.dumps({"doc_type": d["doc_type"], "heading_path": [d["title"]], **meta}),
            ),
        )
    for c in cases:
        if c["location"] == "log":
            e = c["log"]
            conn.execute(
                "INSERT INTO log_events (ts, service, level, message, source, corpus)"
                " VALUES (%s, %s, %s, %s, %s, 'injection')",
                (e["ts"], e["service"], e["level"], e["message"], f"injection:{c['id']}"),
            )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=("load",))
    ap.parse_args(argv)
    cases = load_cases()
    with psycopg.connect(get_settings().database_url) as conn:
        load_corpus(conn, cases)
        conn.commit()
    print(f"loaded {len(cases)} injection cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
