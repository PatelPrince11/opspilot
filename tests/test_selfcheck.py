"""`make selfcheck`: a clean draft passes every check, and each planted flaw fails the right
one. The fixture is a copy of a committed write-up (INC-0055) and its log spec in a temp dir;
selfcheck compares a draft with every write-up except its own id, so the copy is clean."""

import shutil

import pytest

from scripts.corpus import packet, progress, selfcheck
from scripts.corpus.checks import prose_ngrams
from scripts.corpus.common import DATA
from scripts.corpus.runbooks import parse_runbook

INC = "INC-0055"


@pytest.fixture
def draft(tmp_path):
    (tmp_path / "logs").mkdir()
    shutil.copy(DATA / "incidents" / f"{INC}.md", tmp_path / f"{INC}.md")
    shutil.copy(DATA / "incidents" / "logs" / f"{INC}.toml", tmp_path / "logs" / f"{INC}.toml")
    return tmp_path / f"{INC}.md"


def failing(path):
    results, _ = selfcheck.run(INC, path)
    return {name for name, errors in results if errors}


def plant(path, sentence):
    path.write_text(path.read_text() + f"\n{sentence}\n")


def copied_sentence():
    """A real 8-word prose run of RB-011 (all plain words, so no host or code check trips)."""
    body = parse_runbook(DATA / "runbooks" / "RB-011.md")["body"]
    run = next(g for g in sorted(prose_ngrams(body)) if all(w.isalpha() for w in g))
    return " ".join(run).capitalize() + "."


def test_clean_fixture_passes_every_check(draft, capsys):
    assert failing(draft) == set()
    assert selfcheck.main([INC, "--file", str(draft)]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and out.count("PASS") == len(selfcheck.run(INC, draft)[0])


@pytest.mark.parametrize(
    ("sentence", "check"),
    [
        ("Seen on payments-db-01.prod.internal as well.", "hosts"),
        ("Nothing was lost.", "blocklist"),
        (None, "8-word runs"),
    ],
)
def test_planted_flaw_fails_its_check(draft, sentence, check, capsys):
    plant(draft, sentence or copied_sentence())
    assert check in failing(draft)
    assert selfcheck.main([INC, "--file", str(draft)]) == 1
    assert f"FAIL  {check}" in capsys.readouterr().out


def test_missing_log_spec_fails(draft):
    (draft.parent / "logs" / f"{INC}.toml").unlink()
    assert "log spec" in failing(draft)


def test_packet_has_row_sidecar_dates_facts_notes_and_runbooks():
    text = packet.build("INC-0042")
    assert "service = 'catalog-api'" in text and "mechanism = 'migration_lock_queue'" in text
    assert "RB-028: written about this incident (2025-05-15); label only" in text
    assert "**catalog-api / catalog-db** (RB-028)" in text and "**Platform-wide**" in text
    assert "NOTES.md:" in text and "## Runbook RB-012 (full)" in text


def test_progress_resume_rule(tmp_path):
    p = {"batch": 7, "rows": {}}
    assert progress.next_step(p, "INC-0042") == "drafted" and progress.may_redraft(p, "INC-0042")
    progress.set_state(p, "INC-0042", "in_data", note='copied "as is"')
    assert progress.next_step(p, "INC-0042") == "reviewed"
    assert not progress.may_redraft(p, "INC-0042")
    with pytest.raises(ValueError, match="never moved back"):
        progress.set_state(p, "INC-0042", "drafted")
    path = tmp_path / "batch-7" / "progress.toml"
    progress.save(path, p)
    assert progress.load(path) == p
    path.write_text('batch = 7\n[rows."INC-0042"]\nstate = "done"\n')
    with pytest.raises(ValueError, match="not in"):
        progress.load(path)


def test_packet_has_rollout_pacing_and_f5_for_a_vm_bad_deploy_row():
    text = packet.build("INC-0025")
    assert "Before 2026-08-20: a fixed 5-minute wait between hosts" in text
    assert "**F5:** a rollout-tool run can be scoped to a subset of hosts" in text


def test_packet_has_the_proxy_rollout_fact():
    assert "**maxSurge 0 / maxUnavailable 1**" in packet.build("INC-0177")


def test_two_drafts_sharing_an_8_word_run_both_fail_with_drafts(tmp_path):
    (tmp_path / "logs").mkdir()
    shared = "The amber lamps along the quiet corridor glowed softly all evening."
    for inc in ("INC-0055", "INC-0067"):
        shutil.copy(DATA / "incidents" / f"{inc}.md", tmp_path / f"{inc}.md")
        shutil.copy(DATA / "incidents" / "logs" / f"{inc}.toml", tmp_path / "logs" / f"{inc}.toml")
        plant(tmp_path / f"{inc}.md", shared)
    for inc, other in (("INC-0055", "INC-0067"), ("INC-0067", "INC-0055")):
        path = tmp_path / f"{inc}.md"
        alone, _ = selfcheck.run(inc, path)
        assert not dict(alone)["8-word runs"], inc  # no draft-vs-draft check without DRAFTS
        results, _ = selfcheck.run(inc, path, drafts=tmp_path)
        assert any(f"draft:{other}" in e for e in dict(results)["8-word runs"]), inc
        assert selfcheck.main([inc, "--file", str(path), "--drafts", str(tmp_path)]) == 1
