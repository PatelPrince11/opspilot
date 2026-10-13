"""Ranked chunks for a query: dense, keyword, hybrid (RRF) or hybrid + cross-encoder rerank.

    retrieve("payments-api 503s after the deploy", RetrievalConfig(mode="hybrid", k=10))

- dense: bge-small cosine similarity over the HNSW index. The query gets bge's search prefix
  when bge_prefix is on; passages never do.
- keyword: BM25 (k1 1.2, b 0.75) over the chunks matching the query's ORed non-stopword
  tokens (opspilot_bm25; IDF from lexeme_df, rebuilt by every ingest).
- keyword_tsrank: the same candidates ranked by ts_rank_cd (no IDF); kept for the ablation.
- hybrid: reciprocal rank fusion (k=60) of the dense and keyword (BM25) top `candidates`.
- hybrid_rerank: the top `candidates` fused chunks, re-scored by the cross-encoder.

Filters apply inside each SQL query, before ranking. The date filter uses the document's date
(an incident's start day, a runbook's last_reviewed). A severity filter keeps incidents only,
because runbooks have no severity.
"""

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal

import psycopg

from config import get_settings
from rag import embeddings

Mode = Literal["dense", "keyword", "keyword_tsrank", "hybrid", "hybrid_rerank"]
MODES = ("dense", "keyword", "keyword_tsrank", "hybrid", "hybrid_rerank")
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
RRF_K = 60
COLS = "c.id, c.document_id, c.chunk_index, c.content, c.metadata"


@dataclass(frozen=True)
class RetrievalConfig:
    mode: Mode = field(default_factory=lambda: get_settings().retrieval_mode)
    k: int = 10
    candidates: int = field(default_factory=lambda: get_settings().retrieval_candidates)
    bge_prefix: bool = field(default_factory=lambda: get_settings().bge_prefix)
    service: str | None = None
    severity: str | None = None
    doc_type: str | None = None
    since: date | None = None  # inclusive
    until: date | None = None  # inclusive


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    document_id: str
    chunk_index: int
    content: str
    metadata: dict
    score: float


def filters(cfg: RetrievalConfig) -> tuple[str, str, list]:
    """(extra joins, extra AND conditions, their params)."""
    join, conds, params = "", [], []
    if cfg.service:
        conds.append("c.metadata->'services' ? %s")
        params.append(cfg.service)
    if cfg.doc_type:
        conds.append("c.metadata->>'doc_type' = %s")
        params.append(cfg.doc_type)
    if cfg.severity:
        join = " JOIN documents d ON d.id = c.document_id JOIN incidents i ON i.id = d.incident_id"
        conds.append("i.severity = %s")
        params.append(cfg.severity)
    if cfg.since:
        conds.append("(c.metadata->>'date')::date >= %s")
        params.append(cfg.since)
    if cfg.until:
        conds.append("(c.metadata->>'date')::date <= %s")
        params.append(cfg.until)
    return join, "".join(f" AND {c}" for c in conds), params


def dense(conn, query: str, cfg: RetrievalConfig, n: int) -> list[Hit]:
    text = BGE_QUERY_PREFIX + query if cfg.bge_prefix else query
    vec = embeddings.as_pgvector(embeddings.embed([text])[0])
    join, where, params = filters(cfg)
    # An HNSW scan keeps a candidate list of ef_search entries and returns at most that many
    # rows, so ef_search below n would cut a LIMIT n result short. strict_order iterative scan
    # (pgvector 0.8) keeps searching when a filter rejects rows, still in exact distance order.
    conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(max(n, 40)),))
    conn.execute("SELECT set_config('hnsw.iterative_scan', 'strict_order', true)")
    rows = conn.execute(
        f"SELECT {COLS}, 1 - (c.embedding <=> %s::vector) FROM chunks c{join}"
        f" WHERE true{where} ORDER BY c.embedding <=> %s::vector LIMIT %s",
        [vec, *params, vec, n],
    ).fetchall()
    return [Hit(*r) for r in rows]


def keyword(conn, query: str, cfg: RetrievalConfig, n: int) -> list[Hit]:
    """BM25. The scores use corpus-wide stats, so filters only drop rows, never rescore."""
    join, where, params = filters(cfg)
    rows = conn.execute(
        f"SELECT {COLS}, b.score FROM opspilot_bm25(%s) b JOIN chunks c ON c.id = b.chunk_id"
        f"{join} WHERE true{where} ORDER BY b.score DESC, c.id LIMIT %s",
        [query, *params, n],
    ).fetchall()
    return [Hit(*r) for r in rows]


def keyword_tsrank(conn, query: str, cfg: RetrievalConfig, n: int) -> list[Hit]:
    join, where, params = filters(cfg)
    rows = conn.execute(
        f"SELECT {COLS}, ts_rank_cd(c.tsv, q) AS rank FROM chunks c{join},"
        f" opspilot_or_tsquery(%s) q WHERE c.tsv @@ q{where} ORDER BY rank DESC, c.id LIMIT %s",
        [query, *params, n],
    ).fetchall()
    return [Hit(*r) for r in rows]


def rrf(*ranked: list[Hit], k: int = RRF_K) -> list[Hit]:
    """Sum of 1/(k + rank) over the lists. Ranks only, so the two score scales never meet."""
    score: dict[int, float] = {}
    first: dict[int, Hit] = {}
    for hits in ranked:
        for rank, h in enumerate(hits, 1):
            score[h.chunk_id] = score.get(h.chunk_id, 0.0) + 1 / (k + rank)
            first.setdefault(h.chunk_id, h)
    order = sorted(score, key=lambda c: (-score[c], c))
    return [replace(first[c], score=score[c]) for c in order]


def rerank(query: str, hits: list[Hit]) -> list[Hit]:
    if not hits:
        return []
    scores = embeddings.reranker().predict([(query, h.content) for h in hits])
    rescored = [replace(h, score=float(s)) for h, s in zip(hits, scores, strict=True)]
    return sorted(rescored, key=lambda h: (-h.score, h.chunk_id))


def retrieve(
    query: str, config: RetrievalConfig | None = None, conn: psycopg.Connection | None = None
) -> list[Hit]:
    """Top config.k chunks, best first. Opens its own connection when none is given."""
    cfg = config or RetrievalConfig()
    if cfg.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {cfg.mode!r}")
    n = max(cfg.candidates, cfg.k)
    own = conn is None
    conn = conn or psycopg.connect(get_settings().database_url)
    try:
        with conn.transaction():  # set_config(..., true) lasts until this transaction ends
            if cfg.mode == "dense":
                hits = dense(conn, query, cfg, n)
            elif cfg.mode == "keyword":
                hits = keyword(conn, query, cfg, n)
            elif cfg.mode == "keyword_tsrank":
                hits = keyword_tsrank(conn, query, cfg, n)
            else:
                hits = rrf(dense(conn, query, cfg, n), keyword(conn, query, cfg, n))[:n]
        if cfg.mode == "hybrid_rerank":
            hits = rerank(query, hits)
    finally:
        if own:
            conn.close()
    return hits[: cfg.k]
