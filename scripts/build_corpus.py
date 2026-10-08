"""Build the OpsPilot corpus files from the hand-written sources in data/.

Writes files only; loading them into Postgres is `make ingest` (Phase 1 step 5).
Runs in the app container via `make corpus`; `make corpus-check` validates the sources
and checks the committed outputs are up to date, without writing or downloading.

Committed outputs (hashed in MANIFEST.json): data/processed/services.json and
runbooks.jsonl. Incidents and synthetic logs join this list in later batches.
Not committed: LogHub-derived noise (license requires its notice in every copy).
"""

import argparse
import sys
from datetime import UTC

from scripts.corpus import loghub
from scripts.corpus.common import (
    DATA,
    PROCESSED,
    RAW,
    SEED,
    json_bytes,
    jsonl_bytes,
    rng_for,
    sha256_bytes,
)
from scripts.corpus.incidents import load_skeleton, validate_skeleton
from scripts.corpus.runbooks import load_runbooks, runbooks_output, validate_runbooks
from scripts.corpus.services import CorpusError, load_services, services_output, validate_services

MANIFEST = "MANIFEST.json"
LOGHUB_OUT = "log_events.loghub.jsonl"


def build_committed() -> tuple[dict[str, bytes], dict]:
    """Every committed output as {filename: bytes}, plus counts for the manifest."""
    doc = load_services(DATA / "services.toml")
    validate_services(doc)
    services = services_output(doc)
    runbooks = load_runbooks(DATA / "runbooks")
    validate_runbooks(runbooks, doc)
    rb_rows = runbooks_output(runbooks)
    # Skeleton rows are validated now; incidents.jsonl joins the outputs once prose exists.
    skeleton = load_skeleton(DATA / "incidents" / "skeleton.toml")
    validate_skeleton(skeleton, doc, {r["id"] for r in rb_rows})
    outputs = {"services.json": json_bytes(services), "runbooks.jsonl": jsonl_bytes(rb_rows)}
    counts = {
        "services": len(services),
        "runbooks": len(rb_rows),
        "incidents": 0,
        "synthetic_log_events": 0,
    }
    return outputs, counts


def manifest(outputs: dict[str, bytes], counts: dict) -> bytes:
    return json_bytes(
        {
            "seed": SEED,
            "synthetic": True,
            "counts": counts,
            "files": {name: sha256_bytes(data) for name, data in sorted(outputs.items())},
            "loghub": {
                "repo": loghub.REPO,
                "commit": loghub.COMMIT,
                "files": loghub.FILES,
                "note": "background noise only; downloaded at build time, not committed",
            },
        }
    )


def incident_windows() -> list:
    """(key, start, end) windows for LogHub noise. Empty until incidents exist (batch 5)."""
    return []


def iso(ts) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run(check: bool) -> int:
    outputs, counts = build_committed()
    outputs_with_manifest = {**outputs, MANIFEST: manifest(outputs, counts)}

    if check:
        stale = [
            name
            for name, data in outputs_with_manifest.items()
            if not (PROCESSED / name).exists() or (PROCESSED / name).read_bytes() != data
        ]
        if stale:
            print(f"corpus-check: out of date: {stale}; run `make corpus`", file=sys.stderr)
            return 1
        print(f"corpus-check: ok ({counts})")
        return 0

    PROCESSED.mkdir(parents=True, exist_ok=True)
    for name, data in outputs_with_manifest.items():
        (PROCESSED / name).write_bytes(data)

    lh_events = loghub.parse(loghub.fetch_all(RAW / "loghub"))
    placed = loghub.place_in_windows(lh_events, incident_windows(), rng_for)
    (PROCESSED / LOGHUB_OUT).write_bytes(jsonl_bytes({**e, "ts": iso(e["ts"])} for e in placed))

    print(f"corpus: wrote {sorted(outputs_with_manifest)} to data/processed/")
    print(f"corpus: counts {counts}")
    print(
        f"corpus: LogHub {loghub.DATASET}@{loghub.COMMIT[:7]}: {len(lh_events)} lines parsed, "
        f"{len(placed)} placed into {len(incident_windows())} incident windows"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="validate and compare; write and download nothing"
    )
    args = parser.parse_args()
    try:
        return run(args.check)
    except (CorpusError, loghub.LogHubIntegrityError) as exc:
        print(f"corpus: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
