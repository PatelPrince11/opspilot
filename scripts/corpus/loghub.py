"""LogHub OpenStack sample: pinned download, integrity check, parse, and rebasing.

Background noise only. Rows never belong to an incident, are never cited and never
answer an eval question. The data stays out of git: the LogHub license requires its
notice in every copy, so we download at build time instead of redistributing.
"""

import csv
import io
import random
import re
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.corpus.common import sha256_bytes

REPO = "https://github.com/logpai/loghub"
COMMIT = "dd61d0952749ee7963bde24220d1be5ede023033"  # master on 2026-10-03
DATASET = "OpenStack"
FILES = {
    # published path in the repo -> sha256 of the file at COMMIT
    "OpenStack/OpenStack_2k.log_structured.csv": (
        "ecc88e7909777181824260e9bdacd1469bd24d55131d6fc14e9c8c17edc49ff0"
    ),
    "LICENSE": "9ffa6ae259833cdc6e7ed8a0219fec72ef6455fb457b3c44096cb1ac1f0696d4",
}
CSV_PATH = "OpenStack/OpenStack_2k.log_structured.csv"
SOURCE = f"loghub:{DATASET}"
LEVELS = {"INFO": "INFO", "WARNING": "WARN", "ERROR": "ERROR", "DEBUG": "DEBUG"}
CITATION = (
    "Jieming Zhu, Shilin He, Pinjia He, Jinyang Liu, Michael R. Lyu. Loghub: A Large "
    "Collection of System Log Datasets for AI-driven Log Analytics. In ISSRE, 2023."
)


class LogHubIntegrityError(RuntimeError):
    """A downloaded file does not match its pinned hash."""


def raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/logpai/loghub/{COMMIT}/{path}"


def _http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def ensure_file(
    dest: Path, url: str, expected_sha256: str, fetch: Callable[[str], bytes] = _http_get
) -> bytes:
    """Return the file's bytes, reusing a cached copy only if its hash matches.

    A mismatched download raises and writes nothing, so a changed upstream fails loudly
    instead of silently changing the corpus.
    """
    if dest.exists():
        data = dest.read_bytes()
        if sha256_bytes(data) == expected_sha256:
            return data
    data = fetch(url)
    actual = sha256_bytes(data)
    if actual != expected_sha256:
        raise LogHubIntegrityError(f"{url}: sha256 {actual}, expected {expected_sha256}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return data


def fetch_all(raw_dir: Path, fetch: Callable[[str], bytes] = _http_get) -> bytes:
    """Download (or reuse) every pinned file; return the CSV bytes."""
    out = {}
    for path, digest in FILES.items():
        out[path] = ensure_file(raw_dir / Path(path).name, raw_url(path), digest, fetch)
    return out[CSV_PATH]


def parse(csv_bytes: bytes) -> list[dict]:
    """Structured CSV rows -> log events in our schema, original timestamps (UTC assumed).

    service comes from the source log file (nova-api.log.1... -> openstack-nova-api);
    none of these names are in our services table, which log_events allows (no FK).
    """
    events = []
    for row in csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))):
        ts = datetime.strptime(f"{row['Date']} {row['Time']}", "%Y-%m-%d %H:%M:%S.%f")
        component = row["Logrecord"].split(".log")[0]
        events.append(
            {
                "ts": ts.replace(tzinfo=UTC),
                "service": f"openstack-{component}",
                "level": LEVELS[row["Level"]],
                "message": row["Content"],
                "source": SOURCE,
                "incident_id": None,
                "corpus": "main",
            }
        )
    return events


PER_WINDOW = 10  # 200 incidents x 10 lines = the 2,000-line sample, each line used once
INCIDENT_KEY_RE = re.compile(r"^INC-(\d{4})$")  # slots are by incident number only


def place_in_windows(
    events: list[dict],
    windows: list[tuple[str, datetime, datetime]],
    rng_for: Callable[[str], random.Random],
    per_window: int = PER_WINDOW,
) -> list[dict]:
    """Copy a fixed, non-overlapping slice of LogHub lines into each (incident id, start, end)
    window.

    Slices are allocated sequentially by incident number: INC-NNNN gets lines
    [(NNNN - 1) * per_window, NNNN * per_window), so no noise line is used twice and a
    window's noise never changes when other incidents gain write-ups. The sample spans 15
    minutes in 2017, so without rebasing it would never overlap the time ranges query_logs
    searches. Line order inside a slice is kept; timestamps are seeded random offsets inside
    the window. incident_id stays None: noise overlaps an incident's time range but is never
    part of it.
    """
    placed = []
    for key, start, end in windows:
        m = INCIDENT_KEY_RE.fullmatch(key)
        if not m:
            raise ValueError(f"{key}: not an INC-NNNN id")
        first = (int(m.group(1)) - 1) * per_window
        if first < 0 or first + per_window > len(events):
            raise ValueError(f"{key}: no LogHub slice left at line {first} of {len(events)}")
        rng = rng_for(f"loghub:{key}")
        span = (end - start).total_seconds()
        offsets = sorted(rng.uniform(0, span) for _ in range(per_window))
        chunk = events[first : first + per_window]
        for event, offset in zip(chunk, offsets, strict=True):
            placed.append({**event, "ts": start + timedelta(seconds=round(offset, 3))})
    return placed
