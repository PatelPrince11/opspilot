"""Local bge-small embedder and cross-encoder reranker (not LLM calls, so outside llm/client.py).
CPU, pinned revisions."""

from functools import lru_cache

from config import get_settings

BATCH_SIZE = 64


@lru_cache
def model():
    from sentence_transformers import SentenceTransformer  # slow import; only when needed

    s = get_settings()
    return SentenceTransformer(s.embed_model, revision=s.embed_revision, device="cpu")


def count_tokens(texts: list[str]) -> list[int]:
    """WordPiece tokens without [CLS]/[SEP]: what the model sees, minus 2."""
    ids = model().tokenizer(texts, add_special_tokens=False, verbose=False)["input_ids"]
    return [len(x) for x in ids]


@lru_cache
def reranker():
    from sentence_transformers import CrossEncoder

    s = get_settings()
    return CrossEncoder(s.rerank_model, revision=s.rerank_revision, device="cpu")


def embed(texts: list[str]):
    """Unit-length 384-dim vectors, so cosine similarity is a dot product."""
    return model().encode(texts, batch_size=BATCH_SIZE, normalize_embeddings=True)


def as_pgvector(v) -> str:
    """pgvector text literal, as ingestion stores it."""
    return "[" + ",".join(f"{x:.8f}" for x in v) + "]"
