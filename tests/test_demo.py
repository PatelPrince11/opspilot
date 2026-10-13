"""scripts/demo.py's smoke sample: dev only, stratified by difficulty, seeded."""

from scripts.demo import dev_questions, pick_dev


def test_pick_dev_is_stratified_seeded_and_dev_only():
    dev = dev_questions()
    ids = pick_dev(5, seed=0)
    assert ids == pick_dev(5, seed=0) and len(set(ids)) == 5 and set(ids) <= set(dev)
    # dev has 45 exact-token, 28 multi-doc, 27 paraphrase: largest remainder gives 2, 2, 1.
    assert [dev[i]["difficulty"] for i in ids] == ["exact-token"] * 2 + ["multi-doc"] * 2 + [
        "paraphrase"
    ]
