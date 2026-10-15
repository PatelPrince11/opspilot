"""scripts/reproduce.py exit codes, with the eval and git replaced by fakes (no DB, no LLM)."""

import subprocess

import pytest

from eval import generation_eval as ge
from scripts import reproduce


@pytest.fixture
def fake(monkeypatch):
    """A fake ge.main that drives the (wrapped) run_one and write_results like agent_stage does:
    a run that raises is caught and printed, never re-raised."""
    state = {"raise_on": set(), "write": True, "diff_rc": 0, "diff_calls": []}

    def run_one(q, config, spec):
        if q in state["raise_on"]:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        return {"id": q}

    def write_results(questions, config, split):
        return {"config": config} if state["write"] else None

    def main(argv):
        config = argv[argv.index("--config") + 1]
        for q in ("Q001", "Q002"):
            try:
                ge.run_one(q, config, "spec")
            except Exception:
                pass
        ge.write_results(["Q001", "Q002"], config, "test")
        return 0

    def run(cmd, **kw):
        state["diff_calls"].append(cmd)
        return subprocess.CompletedProcess(cmd, state["diff_rc"])

    monkeypatch.setattr(ge, "run_one", run_one)
    monkeypatch.setattr(ge, "write_results", write_results)
    monkeypatch.setattr(ge, "main", main)
    monkeypatch.setattr(reproduce.subprocess, "run", run)
    return state


def test_clean_replay_exits_0_and_diffs_both_files(fake):
    assert reproduce.main() == 0
    (cmd,) = fake["diff_calls"]
    assert cmd[:3] == ["git", "diff", "--exit-code"]
    assert cmd[-2:] == reproduce.FILES


def test_a_run_that_raised_exits_1(fake, capsys):
    fake["raise_on"] = {"Q002"}
    assert reproduce.main() == 1
    assert "2 run(s) raised" in capsys.readouterr().out  # Q002 in shipped and in shipped_off


def test_no_result_file_written_exits_1(fake):
    fake["write"] = False
    assert reproduce.main() == 1


def test_result_files_differ_from_head_exits_1(fake):
    fake["diff_rc"] = 1
    assert reproduce.main() == 1


def test_wrappers_are_removed_afterwards(fake):
    before = ge.run_one, ge.write_results
    reproduce.main()
    assert (ge.run_one, ge.write_results) == before
