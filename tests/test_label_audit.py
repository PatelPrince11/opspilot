"""eval/label_audit.py: pack layout and parsing Gemini's pasted lines."""

import re

from eval.label_audit import INSTRUCTIONS, pack, parse, report
from eval.validate_questions import QUESTIONS, load

FIXTURE = __import__("pathlib").Path(__file__).parent / "fixtures" / "gemini_audit_output.txt"


def questions():
    return load(QUESTIONS)


def test_parse_keeps_good_lines_and_rejects_malformed():
    qs = questions()
    rows, bad, absent = parse(FIXTURE.read_text(), qs)
    assert sorted(rows) == ["Q001", "Q002", "Q003", "Q004"]
    assert rows["Q002"]["missing"] == ["RB-014"] and rows["Q002"]["facts"] == [2]
    assert len(bad) == 6
    for reason in (
        "not in the format",
        "missing_gold",
        "unsupported_facts",
        "duplicate",
        "unknown",
    ):
        assert any(reason in b for b in bad), reason
    assert absent == [q["id"] for q in qs if q["id"] not in rows]
    assert "Q005" in absent and len(absent) == len(qs) - 4


def test_report_flags_wrong_missing_and_unsupported_and_samples_ok():
    qs = questions()
    good = "\n".join(line for line in FIXTURE.read_text().splitlines()[:5])
    rows, bad, absent = parse(good, qs)
    assert not bad
    text = report(qs, rows, absent)
    assert "Flagged: 2. Sampled OK: 2" in text  # Q002 WRONG, Q003 missing gold; Q001, Q004 OK
    assert "## Q002" in text and "## Q003" in text and "## Q001" in text
    assert text.count("**Prince:** AGREE/DISAGREE, note:") == 4
    assert "No Gemini line for: Q005" in text


def test_pack_writes_corpus_and_batches_of_20(tmp_path):
    qs = questions()[:45]
    paths = pack(qs, tmp_path)
    corpus = paths[0].read_text()
    headers = re.findall(r"^=== (INC-\d{4}|RB-\d{3}) ===$", corpus, re.M)
    assert len(headers) == len(set(headers)) == 147 and "INC-0185" in headers
    assert "INC-LIVE-001" not in corpus and "=== INC-0002 ===" not in corpus
    assert [p.name for p in paths[1:]] == ["batch_01.md", "batch_02.md", "batch_03.md"]
    first = paths[1].read_text()
    assert first.startswith(INSTRUCTIONS) and first.count("\nQuestion: ") == 20
    assert "Key facts:\n  1. " in first
