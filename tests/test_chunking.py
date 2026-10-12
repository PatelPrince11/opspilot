"""rag/chunking.py with a whitespace token counter (no model download)."""

from rag.chunking import chunk


def words(texts):
    return [len(t.split()) for t in texts]


def test_runbook_splits_on_headings_and_keeps_paths():
    body = "# Title\n\n## Background\n\nSome text here.\n\n## Fix\n\nDo the thing."
    out = chunk(body, words, size=400, overlap=50, by_heading=True)
    assert [c["heading_path"] for c in out] == [["Title", "Background"], ["Title", "Fix"]]
    assert out[0]["content"].startswith("# Title\n\n## Background")


def test_code_block_is_not_cut_and_overlap_is_prose():
    prose = " ".join(f"Sentence {i} ends here." for i in range(20))  # 80 words
    code = "```\n" + "\n".join(f"cmd {i} arg" for i in range(30)) + "\n```"  # 90 words
    out = chunk(f"{prose}\n\n{code}", words, size=100, overlap=10, by_heading=False)
    assert any(c["content"].endswith(code) for c in out)
    assert all(c["content"].count("```") in (0, 2) for c in out)
    assert all(c["token_count"] <= 100 for c in out)


def test_long_prose_overlaps_and_respects_size():
    prose = " ".join(f"Line {i} has five words." for i in range(100))
    out = chunk(prose, words, size=50, overlap=10, by_heading=False)
    assert all(c["token_count"] <= 50 for c in out)
    assert out[1]["content"].split(".")[0] in out[0]["content"]  # starts with overlap
    assert "Line 99 has five words." in out[-1]["content"]


def test_numbered_list_marker_is_not_a_sentence():
    out = chunk("**3. Look at it.** Then go.", words, size=400, overlap=0, by_heading=False)
    assert out[0]["content"] == "**3. Look at it.** Then go."
