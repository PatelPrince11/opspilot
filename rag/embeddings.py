"""Local bge-small embedder (not an LLM call, so outside llm/client.py). CPU, pinned revision."""

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


def embed(texts: list[str]):
    """Unit-length 384-dim vectors, so cosine similarity is a dot product."""
    return model().encode(texts, batch_size=BATCH_SIZE, normalize_embeddings=True)
