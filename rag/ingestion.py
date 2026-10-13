"""Load data/processed into Postgres, then chunk and embed the documents (`make ingest`).

Order follows the foreign keys: services, incidents, log_events, documents, chunks. Only
data/processed is read: it already holds just the 117 written incidents, and nothing from
eval/ (the sidecar), NOTES, docs/ or the demo spec ever reaches it.

Idempotent. services and incidents are upserted. log_events is reloaded in file order with
its identity restarted, so ids repeat. A document is skipped when its content_hash (text,
metadata, chunk settings, model revision) is unchanged, so its chunk ids stay; a changed
document is deleted (chunks cascade) and rebuilt. Rows no longer in the corpus are deleted.
"""

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from statistics import mean, median

import psycopg

from config import get_settings
from rag import embeddings
from rag.chunking import chunk
from scripts.corpus.common import DATA, PROCESSED
from scripts.corpus.incidents import code_finder
from scripts.corpus.services import load_services

LOG_FILES = ("log_events.synthetic.jsonl", "log_events.loghub.jsonl", "log_events.live.jsonl")
TABLES = ("services", "incidents", "log_events", "documents", "chunks")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def check_inputs(processed: Path) -> None:
    if not (processed / "log_events.loghub.jsonl").exists():
        sys.exit(
            f"{processed / 'log_events.loghub.jsonl'} is missing. It is downloaded at build "
            "time and not committed: run `make corpus` first, then `make ingest`."
        )


def documents(processed: Path) -> list[dict]:
    """Runbooks and incident write-ups with the metadata retrieval filters on."""
    svc = json.loads((processed / "services.json").read_text())
    hosts = sorted({h["name"] for s in svc for h in s["hosts"]})
    codes = code_finder(load_services(DATA / "services.toml"))
    docs = []
    for r in read_jsonl(processed / "runbooks.jsonl"):
        meta = {
            "services": r["services"],
            "failure_types": r["failure_types"],
            "hosts": [h for h in hosts if re.search(rf"(?<![\w-]){re.escape(h)}", r["body"])],
            "codes": sorted(codes(r["body"])[0]),
            "format": r["format"],
            "date": r["last_reviewed"],
        }
        docs.append(dict(r, meta=meta, incident_id=None, path=f"data/runbooks/{r['id']}.md"))
    for r in read_jsonl(processed / "incidents.jsonl"):
        meta = {
            "services": [r["service"], *r["symptom_services"]],
            "failure_types": r["failure_types"],
            "hosts": r["hosts"],
            "codes": r["codes"],
            "format": r["format"],
            "date": r["started_at"][:10],
        }
        docs.append(dict(r, meta=meta, incident_id=r["id"], path=f"data/incidents/{r['id']}.md"))
    return docs


CHUNKER = (Path(__file__).parent / "chunking.py").read_bytes()  # a chunker edit re-chunks


def content_hash(doc: dict, size: int, overlap: int, model: str, revision: str) -> str:
    chunker = hashlib.sha256(CHUNKER).hexdigest()
    key = [doc["title"], doc["body"], doc["meta"], size, overlap, model, revision, chunker]
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


def load_services_table(conn, svc: list[dict]) -> None:
    for s in svc:
        conn.execute(
            "INSERT INTO services (name, owner, dependencies, status) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (name) DO UPDATE SET owner = EXCLUDED.owner,"
            " dependencies = EXCLUDED.dependencies, status = EXCLUDED.status",
            (s["name"], s["owner"], s["dependencies"], s["status"]),
        )


def load_incidents(conn, rows: list[dict]) -> None:
    ids = dict(conn.execute("SELECT name, id FROM services").fetchall())
    for r in rows:
        meta = {
            k: r[k]
            for k in ("title", "format", "failure_types", "hosts", "codes", "symptom_services")
        }
        conn.execute(
            "INSERT INTO incidents (id, service_id, severity, started_at, resolved_at, symptoms,"
            " root_cause, resolution, runbook_ids, synthetic, metadata)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (id) DO UPDATE SET service_id = EXCLUDED.service_id,"
            " severity = EXCLUDED.severity, started_at = EXCLUDED.started_at,"
            " resolved_at = EXCLUDED.resolved_at, symptoms = EXCLUDED.symptoms,"
            " root_cause = EXCLUDED.root_cause, resolution = EXCLUDED.resolution,"
            " runbook_ids = EXCLUDED.runbook_ids, synthetic = EXCLUDED.synthetic,"
            " metadata = EXCLUDED.metadata",
            (
                r["id"],
                ids[r["service"]],
                r["severity"],
                r["started_at"],
                r["resolved_at"],
                r["symptoms"],
                r["root_cause"],
                r["resolution"],
                r["runbook_ids"],
                r["synthetic"],
                json.dumps(meta),
            ),
        )


def load_logs(conn, processed: Path) -> int:
    # ponytail: reloads the whole table each run (~10k rows, under a second). If an
    # injection corpus ever adds log rows, scope this to corpus='main'.
    conn.execute("TRUNCATE log_events RESTART IDENTITY")
    cols = ("ts", "service", "level", "message", "source", "incident_id", "corpus")
    n = 0
    with conn.cursor() as cur:
        with cur.copy(f"COPY log_events ({', '.join(cols)}) FROM STDIN") as cp:
            for name in LOG_FILES:
                for e in read_jsonl(processed / name):
                    cp.write_row([e[c] for c in cols])
                    n += 1
    return n


def load_documents(conn, docs: list[dict]) -> dict:
    s = get_settings()
    stored = dict(conn.execute("SELECT id, content_hash FROM documents").fetchall())
    todo = []
    for d in docs:
        h = content_hash(d, s.chunk_size, s.chunk_overlap, s.embed_model, s.embed_revision)
        if stored.get(d["id"]) != h:
            todo.append((d, h))

    rows = []  # (doc, hash, chunks)
    for d, h in todo:
        parts = chunk(
            d["body"],
            embeddings.count_tokens,
            s.chunk_size,
            s.chunk_overlap,
            by_heading=d["doc_type"] == "runbook",
        )
        rows.append((d, h, parts))
    texts = [p["content"] for _, _, parts in rows for p in parts]
    t0 = time.perf_counter()
    vectors = iter(embeddings.embed(texts)) if texts else iter(())
    embed_s = time.perf_counter() - t0

    for d, h, parts in rows:
        conn.execute("DELETE FROM documents WHERE id = %s", (d["id"],))
        conn.execute(
            "INSERT INTO documents (id, doc_type, title, source_path, content_hash, corpus,"
            " incident_id, synthetic, metadata) VALUES (%s, %s, %s, %s, %s, 'main', %s, %s, %s)",
            (
                d["id"],
                d["doc_type"],
                d["title"],
                d["path"],
                h,
                d["incident_id"],
                d["synthetic"],
                json.dumps(d["meta"]),
            ),
        )
        for i, p in enumerate(parts):
            meta = {"doc_type": d["doc_type"], "heading_path": p["heading_path"], **d["meta"]}
            vec = embeddings.as_pgvector(next(vectors))
            conn.execute(
                "INSERT INTO chunks (document_id, chunk_index, content, token_count, embedding,"
                " metadata) VALUES (%s, %s, %s, %s, %s::vector, %s)",
                (d["id"], i, p["content"], p["token_count"], vec, json.dumps(meta)),
            )
    return {"embedded_docs": len(rows), "embedded_chunks": len(texts), "embed_s": embed_s}


def ingest(conn, processed: Path = PROCESSED) -> dict:
    """Everything in one transaction; the caller commits."""
    check_inputs(processed)
    svc = json.loads((processed / "services.json").read_text())
    incidents = read_jsonl(processed / "incidents.jsonl")
    docs = documents(processed)

    load_services_table(conn, svc)
    load_incidents(conn, incidents)
    logs = load_logs(conn, processed)
    stats = load_documents(conn, docs)

    conn.execute(
        "DELETE FROM documents WHERE corpus = 'main' AND NOT id = ANY(%s)",
        ([d["id"] for d in docs],),
    )
    conn.execute("DELETE FROM incidents WHERE NOT id = ANY(%s)", ([r["id"] for r in incidents],))
    conn.execute("DELETE FROM services WHERE NOT name = ANY(%s)", ([s["name"] for s in svc],))
    conn.execute("SELECT opspilot_bm25_refresh()")  # BM25 df table and corpus stats
    stats["log_lines"] = logs
    return stats


def report(conn) -> list[str]:
    out = [
        f"{t}: {conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0]}"  # noqa: S608
        for t in TABLES
    ]
    rows = conn.execute(
        "SELECT d.doc_type, count(*), array_agg(c.token_count) FROM chunks c"
        " JOIN documents d ON d.id = c.document_id GROUP BY d.doc_type ORDER BY 1"
    ).fetchall()
    for doc_type, n, toks in rows:
        out.append(
            f"chunks[{doc_type}]: {n}, tokens min {min(toks)} median {median(toks):g}"
            f" mean {mean(toks):.0f} max {max(toks)}"
        )
    return out


def main() -> int:
    t0 = time.perf_counter()
    with psycopg.connect(get_settings().database_url) as conn:
        stats = ingest(conn)
        conn.commit()
        lines = report(conn)
    s = get_settings()
    print(f"model {s.embed_model}@{s.embed_revision}; chunk {s.chunk_size}/{s.chunk_overlap}")
    print(
        f"embedded {stats['embedded_chunks']} chunks from {stats['embedded_docs']} changed docs"
        f" in {stats['embed_s']:.1f}s; total {time.perf_counter() - t0:.1f}s"
    )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
