"""eval/validate_questions.py against the real corpus (data/processed)."""

import copy
import json

import pytest

from eval.validate_questions import (
    Corpus,
    check_all,
    check_question,
    import_human,
    load,
    split,
)


@pytest.fixture(scope="module")
def corpus():
    return Corpus()


def q(**kw):
    base = {
        "id": "Q001",
        "question": "kafka-01 won't come back after the broker settings change, ledger writes fail",
        "relevant_doc_ids": ["INC-0185", "RB-027"],
        "expected_key_facts": ["kafka-01 failed to start after the pushed setting"],
        "difficulty": "exact-token",
        "author": "claude",
        "split": None,
    }
    return base | kw


def errs(corpus, **kw):
    return check_question(q(**kw), corpus)


def test_a_good_question_passes(corpus):
    assert errs(corpus) == []


def test_gold_ids_must_be_written_docs(corpus):
    assert "not a written" in errs(corpus, relevant_doc_ids=["INC-LIVE-001"])[0]
    assert "not a written" in errs(corpus, relevant_doc_ids=["INC-0002"])[0]  # unwritten row


def test_exact_token_needs_a_code_or_host_from_gold(corpus):
    e = errs(corpus, question="the message broker restarted badly, ledger writes erroring")
    assert any("exact-token" in x for x in e)
    assert errs(corpus, question="LEDGER-E455 everywhere after the broker settings change") == []


def test_multi_doc_needs_two_gold(corpus):
    e = errs(corpus, difficulty="multi-doc", relevant_doc_ids=["INC-0011"])
    assert any("multi-doc" in x for x in e)


def test_five_word_run_from_gold_is_flagged(corpus):
    run = " ".join(corpus.text["INC-0185"].split()[20:26])
    assert any("5-word run" in x for x in errs(corpus, question=f"kafka-01 {run}"))


def test_twin_needs_its_partner_or_a_distinguishing_detail(corpus):
    base = dict(relevant_doc_ids=["INC-0082"], difficulty="paraphrase")
    e = errs(corpus, question="client cert errors from edge-gateway-03 again?", **base)
    assert any("twin of INC-0007" in x for x in e)  # same host: only the date tells them apart
    ok = "client cert errors from edge-gateway-03 in September 2025?"
    assert not [x for x in errs(corpus, question=ok, **base) if "twin" in x]


def test_retelling_lists_both(corpus):
    e = errs(corpus, relevant_doc_ids=["INC-0185"])
    assert any("retells RB-027" in x for x in e)
    procedure = "kafka-01 didn't start after a config push, how do I check the daemon is really up?"
    assert errs(corpus, question=procedure, relevant_doc_ids=["RB-027"]) == []


def test_no_fix_question_for_unanswerable_rows(corpus):
    e = errs(
        corpus,
        question="how was the internal-dns-01 silence on kafka-02 fixed in February 2026?",
        relevant_doc_ids=["INC-0125", "RB-020"],
    )
    assert any("asks for a fix" in x for x in e)


def test_key_facts_never_narrative_colour(corpus):
    e = errs(corpus, expected_key_facts=["team-platform took it"])
    assert any("narrative colour" in x for x in e)


def test_unlabelled_passes_partial_fails_final(corpus):
    blank = q(relevant_doc_ids=[], expected_key_facts=[], difficulty=None, author="prince")
    assert check_question(blank, corpus) == []
    assert any("unlabelled" in x for x in check_all([blank], corpus, final=True))


def test_final_counts(corpus):
    e = check_all([q()], corpus, final=True)
    assert any("need 200" in x for x in e) and any("need 40" in x for x in e)


def test_final_mix_rules(corpus):
    claude = [q(id=f"Q{i:03}", difficulty="paraphrase") for i in range(160)]
    human = [q(id=f"H{i:03}", author="prince") for i in range(40)]  # all exact-token
    e = check_all(claude + human, corpus, final=True)
    assert any("need 30%" in x for x in e)  # 40 of 200 exact-token
    assert any("20 paraphrase" in x for x in e)
    for x in human[:20]:
        x["difficulty"] = "paraphrase"
    for x in claude[:60]:
        x["difficulty"] = "exact-token"
    e = check_all(claude + human, corpus, final=True)
    assert not any("30%" in x or "20 paraphrase" in x for x in e)


def test_split_is_seeded_even_and_stratified():
    qs = []
    for i in range(200):
        diff = ("exact-token", "paraphrase", "multi-doc")[i % 3]
        qs.append(q(id=f"Q{i:03}", difficulty=diff, author="claude" if i < 160 else "prince"))
    a, b = split(qs), split(copy.deepcopy(qs[::-1]))
    assert a == b
    assert sum(v == "dev" for v in a.values()) == 100
    for diff in ("exact-token", "paraphrase", "multi-doc"):
        for author in ("claude", "prince"):
            group = [a[x["id"]] for x in qs if x["difficulty"] == diff and x["author"] == author]
            assert abs(group.count("dev") - group.count("test")) <= 1


def test_import_human_appends_40_unlabelled(tmp_path, corpus):
    src, into = tmp_path / "in.tsv", tmp_path / "q.jsonl"
    into.write_text(json.dumps(q()) + "\n")
    src.write_text(
        "".join(f"{n}\tqueue lag on kafka-02 again, number {n}?\n" for n in range(1, 41))
    )
    new = import_human(src, "gemini", into)
    assert [x["id"] for x in new][:2] == ["H001", "H002"] and len(load(into)) == 41
    assert check_all(load(into), corpus) == []
    with pytest.raises(SystemExit, match="twice"):
        import_human(src, "gemini", into)
    src.write_text("1\tonly one line\n")
    with pytest.raises(SystemExit, match="exactly 40"):
        import_human(src, "prince", tmp_path / "other.jsonl")
