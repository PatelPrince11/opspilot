"""Shared pieces of the corpus build: paths, the seed, deterministic RNG and JSON output."""

import hashlib
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
PROCESSED = DATA / "processed"
RAW = DATA / "raw"
SIDECAR = ROOT / "eval" / "corpus"  # eval-only labels; ingest never reads this

SEED = 20261003


def rng_for(key: str) -> random.Random:
    """One RNG per item (e.g. an incident id), so editing one item never reshuffles the rest.

    hashlib, not hash(): Python salts str hashes per process, which would break determinism.
    """
    digest = hashlib.sha256(f"{SEED}:{key}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(obj) -> bytes:
    """Stable JSON: sorted keys and a trailing newline, so identical input gives identical bytes."""
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def jsonl_bytes(rows) -> bytes:
    return "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows).encode()
