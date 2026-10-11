"""Build the OpsPilot corpus files from the hand-written sources in data/.

Writes files only; loading them into Postgres is `make ingest` (Phase 1 step 5).
Runs in the app container via `make corpus`; `make corpus-check` validates the sources
and checks the committed outputs are up to date, without writing or downloading.

Committed outputs (hashed in MANIFEST.json): data/processed/services.json,
runbooks.jsonl, incidents.jsonl (incidents with a write-up), log_events.synthetic.jsonl and
log_events.live.jsonl (the open demo incident's logs from data/live/: never a document, no noise).
Not committed: LogHub-derived noise (license requires its notice in every copy).
"""

import argparse
import sys
from datetime import UTC, timedelta

from scripts.corpus import loghub, synthetic_logs
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
from scripts.corpus.incidents import (
    incidents_output,
    load_documents,
    load_skeleton,
    validate_documents,
    validate_skeleton,
)
from scripts.corpus.runbooks import load_runbooks, runbooks_output, validate_runbooks
from scripts.corpus.services import CorpusError, load_services, services_output, validate_services

MANIFEST = "MANIFEST.json"
LOGHUB_OUT = "log_events.loghub.jsonl"
SYNTHETIC_OUT = "log_events.synthetic.jsonl"
LIVE_OUT = "log_events.live.jsonl"


def build_committed() -> tuple[dict[str, bytes], dict]:
    """Every committed output as {filename: bytes}, plus counts for the manifest."""
    doc = load_services(DATA / "services.toml")
    validate_services(doc)
    services = services_output(doc)
    runbooks = load_runbooks(DATA / "runbooks")
    validate_runbooks(runbooks, doc)
    rb_rows = runbooks_output(runbooks)
    skeleton = load_skeleton(DATA / "incidents" / "skeleton.toml")
    validate_skeleton(skeleton, doc, {r["id"] for r in rb_rows})
    docs = load_documents(DATA / "incidents")
    validate_documents(docs, skeleton, doc, runbooks)
    inc_rows = incidents_output(docs, skeleton)
    events = synthetic_events(skeleton, docs, doc)
    live = synthetic_logs.live_events(DATA / "live" / "INC-LIVE-001.toml", doc)
    outputs = {
        "services.json": json_bytes(services),
        "runbooks.jsonl": jsonl_bytes(rb_rows),
        "incidents.jsonl": jsonl_bytes(inc_rows),
        SYNTHETIC_OUT: jsonl_bytes({**e, "ts": iso(e["ts"])} for e in events),
        LIVE_OUT: jsonl_bytes({**e, "ts": iso(e["ts"])} for e in live),
    }
    counts = {
        "services": len(services),
        "runbooks": len(rb_rows),
        "incidents": len(inc_rows),
        "synthetic_log_events": len(events),
        "live_log_events": len(live),
    }
    return outputs, counts


def synthetic_events(skeleton: list[dict], docs: list[dict], services_doc: dict) -> list[dict]:
    """Generate and check the logs of every documented incident; each must have a spec."""
    by_id = {r["id"]: r for r in skeleton}
    events = []
    for d in docs:
        row = by_id[d["meta"]["id"]]
        spec_path = DATA / "incidents" / "logs" / f"{row['id']}.toml"
        if not spec_path.exists():
            raise CorpusError(f"{row['id']}: no log spec at {spec_path.relative_to(DATA)}")
        spec = synthetic_logs.load_spec(spec_path)
        evs = synthetic_logs.generate(row["id"], spec, row["started_at"].date())
        synthetic_logs.validate_logs(evs, row, services_doc)
        events += evs
    return events


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
    """(incident id, start - 10 min, end + 10 min) for every incident with a write-up: the
    windows LogHub noise is placed in, the same span its synthetic logs may use."""
    rows = {r["id"]: r for r in load_skeleton(DATA / "incidents" / "skeleton.toml")}
    pad = timedelta(minutes=10)
    return [
        (i, rows[i]["started_at"] - pad, rows[i]["resolved_at"] + pad)
        for i in sorted(d["meta"]["id"] for d in load_documents(DATA / "incidents"))
    ]


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
    windows = incident_windows()
    placed = loghub.place_in_windows(lh_events, windows, rng_for)
    (PROCESSED / LOGHUB_OUT).write_bytes(jsonl_bytes({**e, "ts": iso(e["ts"])} for e in placed))

    print(f"corpus: wrote {sorted(outputs_with_manifest)} to data/processed/")
    print(f"corpus: counts {counts}")
    print(
        f"corpus: LogHub {loghub.DATASET}@{loghub.COMMIT[:7]}: {len(lh_events)} lines parsed, "
        f"{len(placed)} placed into {len(windows)} incident windows"
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
