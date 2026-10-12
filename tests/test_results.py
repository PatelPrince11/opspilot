"""The results ledger: named result files are stable across identical runs, and docs/results.md
renders from committed files without any database."""

import json
import subprocess

import psycopg
import pytest

from eval.retrieval_eval import provenance, write_named
from scripts import results


def test_write_named_leaves_identical_content_untouched(tmp_path):
    path = tmp_path / "dev_locked.json"
    content = {"split": "dev", "metrics": {"overall": {"recall@5": 0.853}}, "questions": []}
    assert write_named(path, content, {"commit": "a" * 40, "latency": {"p95_ms": 53.0}})
    first = path.read_text()
    # Same numbers, new commit and latency: the file (and its provenance) stays as it was.
    assert not write_named(path, content, {"commit": "b" * 40, "latency": {"p95_ms": 99.0}})
    assert path.read_text() == first
    changed = content | {"metrics": {"overall": {"recall@5": 0.9}}}
    assert write_named(path, changed, {"commit": "b" * 40, "latency": {"p95_ms": 99.0}})
    assert json.loads(path.read_text())["commit"] == "b" * 40


def test_clean_rerun_replaces_only_a_dirty_provenance(tmp_path):
    path = tmp_path / "dev_locked.json"
    content = {"split": "dev", "metrics": {"overall": {"recall@5": 0.853}}, "questions": []}
    dirty = {"commit": "a" * 40, "uncommitted_changes": ["x.py"], "latency": {"p95_ms": 53.0}}
    clean = {"commit": "b" * 40, "uncommitted_changes": [], "latency": {"p95_ms": 99.0}}
    write_named(path, content, dirty)
    # A dirty rerun keeps the first provenance.
    assert not write_named(path, content, dirty | {"commit": "c" * 40})
    assert write_named(path, content, clean)
    d = json.loads(path.read_text())
    assert (d["commit"], d["uncommitted_changes"], d["latency"]) == ("b" * 40, [], {"p95_ms": 53.0})
    # Clean now: a later clean rerun changes nothing.
    assert not write_named(path, content, clean | {"commit": "d" * 40})
    # Different numbers are never treated as a provenance fix.
    other = content | {"metrics": {"overall": {"recall@5": 0.9}}}
    assert write_named(path, other, clean)


def test_result_outputs_never_make_a_run_dirty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files = ["eval/results/dev_locked.json", "docs/results.md", "a.py"]
    for f in files:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("1")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True)
    for f in files[:2]:
        (tmp_path / f).write_text("2")
    assert provenance()["uncommitted_changes"] == []
    (tmp_path / "a.py").write_text("2")
    assert provenance()["uncommitted_changes"] == ["a.py"]


def test_ledger_renders_without_a_database(monkeypatch):
    def no_db(*a, **k):
        raise AssertionError("scripts/results.py must not touch the database")

    monkeypatch.setattr(psycopg, "connect", no_db)
    text = results.render()
    assert text == results.render()  # deterministic
    assert "| Recall@5 (mean fraction of gold docs found) |" in text


@pytest.mark.parametrize("name", ["dev_locked.json", "dev_dense_baseline.json"])
def test_named_result_files_have_no_run_ids_or_timestamps(name):
    d = json.loads((results.RESULTS / name).read_text())
    assert set(d) == {
        "commit",
        "config",
        "corpus",
        "latency",
        "metrics",
        "models",
        "questions",
        "split",
        "uncommitted_changes",
    }
    assert len(d["questions"]) == 100 and all("latency_ms" not in q for q in d["questions"])
