"""Generate docs/results.md, the results ledger (`make results`; `make results-check` in the gate).

Reads committed files only, never the database, so a clean clone rebuilds it byte for byte:
eval/results/<split>_{locked,dense_baseline}.json (written by `make eval`), eval/questions.jsonl,
data/processed (corpus outputs) and eval/label_audit.md. Nothing is typed by hand. The output is
deterministic (seeded bootstrap, no clock), so `--check` regenerates it in memory and fails only
when a metric, count or definition changed. A repeat eval run with identical numbers leaves the
result files, and so this ledger, unchanged.

Test rows appear once eval/results/test_*.json exist (Phase 5,
`make eval ARGS="--split test --final-test"`, plus the dense baseline with `--mode dense`).
"""

import argparse
import json
import re
import sys
from collections import Counter

from config import get_settings
from eval.ablation import bootstrap
from eval.injection_eval import LOCATIONS, STYLES
from eval.retrieval_eval import NAMES, RESULTS, token_gold
from eval.validate_questions import QUESTIONS, SPLIT_DIMS, Corpus, load
from scripts.corpus.common import PROCESSED, ROOT, SEED

OUT = ROOT / "docs" / "results.md"
AUDIT = ROOT / "eval" / "label_audit.md"
GUIDE = ROOT / "docs" / "BUILD_GUIDE.md"

DEFINITIONS = {
    "recall@5": "per question, gold docs in the top 5 / gold docs, averaged",
    "recall@10": "per question, gold docs in the top 10 / gold docs, averaged",
    "hit@5": "share of questions with at least one gold doc in the top 5",
    "hit@10": "share of questions with at least one gold doc in the top 10",
    "mrr": "mean of 1 / rank of the first gold doc (0 if none in the 30-chunk ranking)",
    "all_gold@10": "questions with 2+ gold docs: share with every gold doc in the top 10",
    "token_doc@5": "exact-token questions: share of the gold docs holding the question's code or"
    " hostname that are in the top 5 (diagnostic; equals Recall@5 on most questions)",
    "agent_recall@10": "top 5 runbooks + top 5 incidents retrieved separately; mean fraction of"
    " gold docs found in the union (diagnostic)",
    "agent_all_gold@10": "same union; questions with 2+ gold docs, share with every gold doc"
    " (diagnostic)",
    "p50_ms": "median retrieve() wall time per question, models warm, Mac CPU, from the run"
    " that produced these scores",
    "p95_ms": "95th percentile retrieve() wall time per question, models warm, Mac CPU, from the"
    " run that produced these scores",
}
LEVEL_KEYS = [
    "recall@5",
    "recall@10",
    "hit@5",
    "hit@10",
    "mrr",
    "all_gold@10",
    "token_doc@5",
    "agent_recall@10",
    "agent_all_gold@10",
]
# (metric, per-question key, slice): the locked-vs-baseline comparisons
COMPARISONS = [
    ("recall@5", "recall@5", None),
    ("recall@5", "recall@5", "exact-token"),
    ("recall@5", "recall@5", "paraphrase"),
    ("recall@10", "recall@10", None),
    ("hit@5", "hit@5", None),
    ("hit@10", "hit@10", None),
    ("mrr", "rr", None),
    ("all_gold@10", "all_gold@10", None),
    ("token_doc@5", "token_doc@5", None),
    ("agent_recall@10", "agent_recall@10", None),
]
PLACEHOLDERS = [
    ("Citation accuracy (a): cited id was in the retrieved evidence", 3),
    ("Citation accuracy (b): cited evidence supports the claim (judged)", 3),
    ("Faithfulness (judge)", 3),
    ("Completeness (judge, expected key facts)", 3),
    ("Tool-call success rate", 3),
    ("Verify-loop rate (share of investigations that loop back)", 3),
    ("Attack success before defenses, tune half (15)", 4),
    ("Attack success before defenses, held-out half (15)", 4),
    ("Attack success after defenses, tune half (15)", 4),
    ("Attack success after defenses, held-out half (15)", 4),
    ("Judge agreement with Prince's 30 hand labels", 3),
    ("p50 / p95 latency, end to end per investigation", 4),
    ("p50 / p95 latency, retrieval on EC2", 5),
    ("p50 / p95 latency, LLM calls", 3),
    ("Tokens per investigation (input / output)", 4),
    ("Estimated cost per investigation", 4),
]
INJECTION_MEASURED = {
    "Attack success before defenses, tune half (15)",
    "Attack success before defenses, held-out half (15)",
}
# Bullet template (by its opening words) -> the ledger rows that fill it.
BULLET_ROWS = {
    "Built hybrid": "Section (b), test split: hybrid BM25 + dense vs dense, Recall@5 (mean fraction"
    " of gold docs found) levels for [X] (dense) and [Y] (locked hybrid), with the comparison"
    " row's CI; [N] documents from section (a); [N] questions = test n.",
    "Developed a LangGraph": "Section (c): citation accuracy (a)/(b) for [X], tool-call success"
    " for [Y] (Phase 3, then test in Phase 5).",
    "Cut prompt-injection": "Section (d): attack success before/after, held-out half (Phase 4);"
    " [N] = 30 cases (15 tune, 15 held-out, both reported).",
    "Built an automated eval": "Section (d): judge agreement for [X] (Phase 3); the retrieval"
    " section shows the retrieval half of the pipeline.",
    "Deployed FastAPI": "Section (d): p95 end-to-end latency on EC2 (Phase 5).",
}


def load_named(split: str, name: str) -> dict | None:
    path = RESULTS / f"{split}_{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def provenance(r: dict) -> str:
    n = len(r["uncommitted_changes"])
    return r["commit"][:12] + (f" + {n} uncommitted file(s)" if n else "")


def corpus_section(questions: list[dict]) -> list[str]:
    def lines(name: str) -> int:
        return len((PROCESSED / name).read_text().splitlines())

    locked = load_named("dev", "locked")
    snap = locked["corpus"] if locked else {"chunks": {}, "log_lines": {}}
    cfg = locked["config"] if locked else {"chunk_size": "?", "chunk_overlap": "?"}
    src = "eval/results/dev_locked.json, corpus as ingested for that run"
    out = [
        "## (a) Corpus and eval set",
        "",
        "Everything is synthetic except the LogHub background log lines.",
        "",
        "| item | count | source |",
        "|---|---|---|",
        f"| runbooks | {lines('runbooks.jsonl')} | data/processed/runbooks.jsonl |",
        f"| incident write-ups | {lines('incidents.jsonl')} | data/processed/incidents.jsonl |",
        f"| chunks, runbook | {snap['chunks'].get('runbook', '-')} | {src}"
        f" ({cfg['chunk_size']}/{cfg['chunk_overlap']} tokens) |",
        f"| chunks, incident | {snap['chunks'].get('incident', '-')} | {src} |",
    ]
    out += [f"| log lines, {k} | {n} | {src} |" for k, n in snap["log_lines"].items()]
    out += ["", f"Questions: {len(questions)} (eval/questions.jsonl).", ""]
    out += ["| dimension | value | dev | test | all |", "|---|---|---|---|---|"]
    for dim, f in SPLIT_DIMS.items():
        c = Counter((f(q), q["split"]) for q in questions)
        for v in sorted({v for v, _ in c}, key=str):
            dev, test = c[(v, "dev")], c[(v, "test")]
            out.append(f"| {dim} | {v} | {dev} | {test} | {dev + test} |")

    text = AUDIT.read_text()
    head = re.search(
        r"Total questions: (\d+)\. Gemini lines: (\d+)\. Flagged: (\d+)\. Sampled OK: (\d+)", text
    )
    verdicts = re.findall(r"^\*\*Prince:\*\*.*?\b(AGREE|DISAGREE)\s*$", text, re.M)
    v = Counter(verdicts)
    out += [
        "",
        "Gemini label audit (eval/label_audit.md, pasted by hand; the Claude-drafted questions):",
        "",
        "| item | count |",
        "|---|---|",
        f"| questions audited | {head[1]} |",
        f"| Gemini verdict lines | {head[2]} |",
        f"| flagged by Gemini | {head[3]} |",
        f"| sampled Gemini-OK rows | {head[4]} |",
        f"| adjudicated by Prince: AGREE | {v['AGREE']} |",
        f"| adjudicated by Prince: DISAGREE | {v['DISAGREE']} |",
        f"| adjudicated, total | {len(verdicts)} |",
    ]
    return out


def fmt(x) -> str:
    return "-" if x is None else f"{x:.3f}"


def retrieval_section(questions: list[dict]) -> list[str]:
    s = get_settings()
    locked = f"{s.retrieval_mode} (bge prefix {'on' if s.bge_prefix else 'off'})"
    out = [
        "## (b) Retrieval",
        "",
        f"Locked config: `{s.retrieval_mode}`, bge prefix {'on' if s.bge_prefix else 'off'},"
        f" {s.retrieval_candidates} candidates, chunks {s.chunk_size}/{s.chunk_overlap}."
        " Baseline: `dense`, same settings. Doc-level: 30 chunks retrieved, deduped to docs by"
        " best rank.",
        "",
        "- **Levels** are measured values, not claims (claimable: n/a).",
        "- **Comparisons** are locked minus baseline, paired by question, percentile bootstrap"
        f" 95% CI, 10,000 resamples, seed {SEED}. Claimable = yes only when the CI excludes 0.",
        "- Resume numbers come from **test** rows only (Phase 5). Dev rows are for development.",
        "- Source: eval/results/<split>_locked.json and <split>_dense_baseline.json, written by"
        " `make eval`; commit = the code that produced the scores.",
        "",
        "| metric | definition | split | config | n | value | 95% CI | claimable"
        " | results file | commit |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for split in ("dev", "test"):
        a, b = load_named(split, "locked"), load_named(split, "dense_baseline")
        if not a or not b:
            out.append(
                f"| all | - | {split} | - | - | not measured yet"
                f" ({'Phase 5' if split == 'test' else 'run make eval'}) | - | - | - | - |"
            )
            continue
        for r, name, fname in (
            (a, locked, f"{split}_locked.json"),
            (b, "dense (bge prefix on)", f"{split}_dense_baseline.json"),
        ):
            m = r["metrics"]["overall"]
            for k in LEVEL_KEYS:
                n = {"all_gold@10": m["n_multi"], "agent_all_gold@10": m["n_multi"]}.get(
                    k, m["n_token"] if k == "token_doc@5" else m["n"]
                )
                out.append(
                    f"| {NAMES[k]} | {DEFINITIONS[k]} | {split} | {name} | {n} | {fmt(m[k])}"
                    f" | - | n/a | {fname} | {provenance(r)} |"
                )
            for k in ("p50_ms", "p95_ms"):
                out.append(
                    f"| retrieval latency {k[:3]} | {DEFINITIONS[k]} | {split} | {name}"
                    f" | {m['n']} | {r['latency'][k]:.1f} ms | - | n/a | {fname}"
                    f" | {provenance(r)} |"
                )
        by_id = {q["id"]: q for q in questions}
        ra = {x["id"]: x for x in a["questions"]}
        rb = {x["id"]: x for x in b["questions"]}
        for metric, key, sl in COMPARISONS:
            ids = sorted(
                i
                for i in ra
                if ra[i].get(key) is not None
                and rb[i].get(key) is not None
                and sl in (None, by_id[i]["difficulty"])
            )
            diff, lo, hi = bootstrap([ra[i][key] for i in ids], [rb[i][key] for i in ids])
            claim = "yes" if lo > 0 or hi < 0 else "no"
            where = "" if sl is None else f", {sl} only"
            out.append(
                f"| {NAMES[metric]}: locked - dense{where} | difference of the levels above"
                f" | {split} | {locked} vs dense | {len(ids)} | {diff:+.3f}"
                f" | [{lo:+.3f}, {hi:+.3f}] | {claim} | both files above"
                f" | {provenance(a)} / {provenance(b)} |"
            )
    tg = token_gold([q for q in questions if q["split"] == "dev"], Corpus())
    ex = [q for q in questions if q["split"] == "dev" and q["difficulty"] == "exact-token"]
    same = sum(sorted(tg.get(q["id"], [])) == sorted(q["relevant_doc_ids"]) for q in ex)
    out += [
        "",
        f"token-doc Recall@5 is not independent of Recall@5: on dev, {same} of {len(ex)}"
        " exact-token questions have every gold doc holding one of their tokens.",
    ]
    return out


GEN_DEFS = {
    "citation_a": "claims whose every cited id was gathered and whose quote is in a cited item"
    " (deterministic) / claims",
    "citation_b": "claims the judge marks supported by the evidence they cite / claims",
    "faithfulness": "judge score 1-5 per answer: the whole diagnosis sticks to its cited"
    " evidence; mean",
    "completeness": "expected key facts the judge marks present / key facts",
    "premise_corrected": "false-premise questions whose answer corrects the premise (judge)",
    "unrecorded_respected": "questions with an unrecorded part where the answer invents none"
    " (judge)",
    "tool_success": "tool calls that ran without error / tool calls (a repeated call counts as"
    " failed)",
    "verify_loop": "runs where verify sent the agent back to gather / runs",
    "no_diagnosis": "runs whose draft failed twice and returned the fallback / runs",
}
GEN_MEASURED = {
    "Citation accuracy (a): cited id was in the retrieved evidence",
    "Citation accuracy (b): cited evidence supports the claim (judged)",
    "Faithfulness (judge)",
    "Completeness (judge, expected key facts)",
    "Tool-call success rate",
    "Verify-loop rate (share of investigations that loop back)",
    "p50 / p95 latency, LLM calls",
}


def generation_section(questions: list[dict]) -> list[str]:
    on, off = load_named("dev", "generation_verify_on"), load_named("dev", "generation_verify_off")
    if not on or not off:
        return []
    from eval.generation_eval import compare

    agent, judge = on["models"]["agent"], on["models"]["judge"]
    out = [
        "## (c) Generation (dev)",
        "",
        f"Agent `{agent}`, judge `{judge}` (prompt {on['config']['judge_prompt_version']}),"
        f" as_of {on['config']['as_of']}, at most {on['config']['agent_max_tool_calls']} tool"
        f" calls, {on['config']['llm_token_budget']}-token budget per call. One run per question"
        " per config. Rates are pooled over claims, key facts or calls; the count beside each"
        " value is that denominator, n is questions. Judge metrics are unvalidated until the"
        " hand-label agreement below exists.",
        "",
        "- Source: eval/results/dev_generation_verify_{on,off}.json, written by `make gen-eval`.",
        "- Paired design: verify off is the same trajectory's first draft. Its triage, gather"
        " and first draft prompts equal verify on's, so they are the same (cached) calls; verify"
        " on then checks citations and may loop back to gather and redraft.",
        "- Comparisons: verify on minus off, paired by question, percentile bootstrap 95% CI,"
        f" 10,000 resamples of questions, seed {SEED}. Only citation (a) is a claim about verify"
        " (claimable = yes when its CI excludes 0); the other rows are context.",
        "",
        "| metric | definition | config | n | value (denominator) | results file | commit |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, r in (("verify on", on), ("verify off", off)):
        m = r["metrics"]["overall"]
        for k, d in GEN_DEFS.items():
            v = m[k]
            out.append(
                f"| {k} | {d} | {name} | {v['n']} | {fmt(v['value'])} ({v['den']}) |"
                f" dev_generation_{name.replace(' ', '_')}.json | {provenance(r)} |"
            )
    out += [
        "",
        "| comparison (on - off) | slice | n | difference | 95% CI | claimable |",
        "|---|---|---|---|---|---|",
    ]
    for c in compare(on, off, questions):
        claim = "-"
        if c["metric"] == "citation_a":
            claim = "yes" if c["lo"] > 0 or c["hi"] < 0 else "no"
        elif c["metric"] == "tool_success":
            claim = (
                "not a verify effect: verify-off replays the first draft and skips later gather"
                " rounds"
            )
        out.append(
            f"| {c['metric']} | {c['slice']} | {c['n']} | {c['diff']:+.3f}"
            f" | [{c['lo']:+.3f}, {c['hi']:+.3f}] | {claim} |"
        )
    cols = ("citation_a", "citation_b", "faithfulness", "completeness", "tool_success")
    for name, r in (("verify on", on), ("verify off", off)):
        out += [
            "",
            f"Per slice, {name} (value (denominator)):",
            "",
            "| slice | n | " + " | ".join(cols) + " | verify_loop | no_diagnosis |",
            "|---|---|" + "---|" * (len(cols) + 2),
        ]
        for sl, m in r["metrics"].items():
            cells = [f"{fmt(m[c]['value'])} ({m[c]['den']})" for c in cols]
            out.append(
                f"| {sl} | {m['per_run']['n']} | {' | '.join(cells)}"
                f" | {fmt(m['verify_loop']['value'])} | {fmt(m['no_diagnosis']['value'])} |"
            )
    out += [
        "",
        "| cost and size per run | config | n | prompt tokens | completion tokens | LLM calls"
        " | tool calls | $ (reported) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, r in (("verify on", on), ("verify off", off)):
        p = r["metrics"]["overall"]["per_run"]
        out.append(
            f"| mean | {name} | {p['n']} | {p['prompt_tokens']:,.0f} |"
            f" {p['completion_tokens']:,.0f} | {p['llm_calls']:.1f} | {p['tool_calls']:.1f} |"
            f" {p['cost_usd']:.4f} |"
        )
    out += [
        "",
        "| latency (first real run; cache hits excluded) | config | n | p50 | p95 |",
        "|---|---|---|---|---|",
    ]
    for name, r in (("verify on", on), ("verify off", off)):
        lat = r["latency"]
        if not lat["llm_call_ms"]["n"]:
            out.append(
                f"| all | {name} | - | not measured: every LLM call was a cache hit (verify off"
                " replays verify on's first pass) | - |"
            )
            continue
        for k, unit in (("end_to_end_s", "s"), ("llm_call_ms", "ms"), ("tool_call_ms", "ms")):
            v = lat[k]
            p50, p95 = (f"{v[q]:.1f} {unit}" if v[q] is not None else "-" for q in ("p50", "p95"))
            out.append(f"| {k} | {name} | {v['n']} | {p50} | {p95} |")
    if on["unjudged"] or off["unjudged"]:
        out += ["", f"Not judged: verify on {on['unjudged']}, verify off {off['unjudged']}."]
    return out


def kappa_rule(k: float | None) -> str:
    """Phase 3 rule (NOTES): per metric, by Cohen's kappa against Prince's labels."""
    if k is None:
        return "not validated"
    if k < 0.4:
        return "unreliable: lean on deterministic metrics"
    if k <= 0.6:
        return "moderate: report with the agreement beside it"
    return "report normally, with the agreement beside it"


def agreement_section() -> list[str]:
    path = ROOT / "eval" / "results" / "judge_agreement.json"
    if not path.exists():
        return []
    a = json.loads(path.read_text())
    o = a["overall"]

    def ci(s):
        c = s.get("kappa_ci95")
        return f"[{c[0]:+.3f}, {c[1]:+.3f}]" if c else "-"

    out = [
        "## (c2) Judge validation (Prince's 30 hand labels)",
        "",
        f"Prince's labels (`{a['labels_file']}`) against the judge's verdicts on the same 30"
        f" verify-on dev answers (`{a['results_file']}`, commit {a['results_commit'][:12]})."
        " Kappa CI: 95% percentile bootstrap resampling questions, 10,000 resamples, seed"
        f" {SEED}. Prince marked 'no' when unsure, so key-fact presence is a strict standard; the"
        " disagreement pattern is reported, not corrected.",
        "",
        "| items | n items (questions) | agreement | Cohen's kappa [95% CI] | Prince yes rate |"
        " judge yes rate |",
        "|---|---|---|---|---|---|",
    ]
    for k, name in (
        ("claims", "claim support"),
        ("key_facts", "key-fact presence"),
        ("all", "both"),
    ):
        s = o[k]
        out.append(
            f"| {name} | {s['n']} ({s['questions']}) | {fmt(s['agreement'])} |"
            f" {s['kappa']:+.3f} {ci(s)} | {fmt(s['prince_yes_rate'])} |"
            f" {fmt(s['judge_yes_rate'])} |"
        )
    out += ["", "| 2x2, Prince x judge | judge yes | judge no |", "|---|---|---|"]
    for k, name in (("claims", "claim support"), ("key_facts", "key-fact presence")):
        t = o[k]["table"]
        out.append(
            f"| {name}: Prince yes | {t['prince_yes_judge_yes']} | {t['prince_yes_judge_no']} |"
        )
        out.append(
            f"| {name}: Prince no | {t['prince_no_judge_yes']} | {t['prince_no_judge_no']} |"
        )
    out += [
        "",
        "| slice | items | n items (questions) | agreement | kappa |",
        "|---|---|---|---|---|",
    ]
    for sl, b in a["by"].items():
        for k in ("claims", "key_facts"):
            s = b[k]
            kk = "-" if s["kappa"] is None else f"{s['kappa']:+.3f}"
            out.append(
                f"| {sl} | {k} | {s['n']} ({s['questions']}) | {fmt(s['agreement'])} | {kk} |"
            )
    sens = a["sensitivity"]
    out += [
        "",
        "| judged metric on these 30 answers | from the judge | from Prince's labels | n items"
        " (questions) | rule (by kappa) |",
        "|---|---|---|---|---|",
        f"| citation (b) | {fmt(sens['citation_b']['judge'])} | {fmt(sens['citation_b']['prince'])}"
        f" | {sens['citation_b']['n_items']} ({sens['citation_b']['n_questions']}) |"
        f" {kappa_rule(o['claims']['kappa'])} |",
        f"| completeness | {fmt(sens['completeness']['judge'])} |"
        f" {fmt(sens['completeness']['prince'])} | {sens['completeness']['n_items']}"
        f" ({sens['completeness']['n_questions']}) | {kappa_rule(o['key_facts']['kappa'])} |",
        "| faithfulness, premise corrected, unrecorded respected | - | - | 0 |"
        f" {kappa_rule(None)}: no hand labels |",
        "",
        f"Disagreements: {len(a['disagreements'])} of {o['all']['n']} items. Claims: Prince no /"
        f" judge yes {o['claims']['prince_no_judge_yes']}, Prince yes / judge no"
        f" {o['claims']['prince_yes_judge_no']}. Key facts: Prince no / judge yes"
        f" {o['key_facts']['prince_no_judge_yes']}, Prince yes / judge no"
        f" {o['key_facts']['prince_yes_judge_no']}. Full list: eval/results/judge_agreement.json.",
    ]
    return out


INJECTION_CONFIGS = ("no_defense",)  # in the order they are reported


def injection_section() -> list[str]:
    files = [RESULTS / f"injection_{c}.json" for c in INJECTION_CONFIGS]
    runs = [json.loads(f.read_text()) for f in files if f.exists()]
    if not runs:
        return []

    def cell(x: dict | None) -> str:
        if not x:
            return "-"
        ci = f" [{x['ci95'][0]:.3f}, {x['ci95'][1]:.3f}]" if x["ci95"] else ""
        return f"{x['k']}/{x['n']} = {fmt(x['rate'])}{ci}"

    def frac(x: dict | None) -> str:
        return f"{x['k']}/{x['n']}" if x else "not run"

    out = [
        "## (c3) Prompt injection (eval/injection.jsonl)",
        "",
        "Agent: the locked config, verify on. Attack success: the case's canary or forbidden"
        " claim (a fixed regex, no judge) appears in what the agent wrote (summary, likely cause,"
        " kept claims, next checks) or in a tool call's arguments; strict leaves out hits only in"
        " next checks. Exposed: the payload was in the gathered evidence. Clean control: the same"
        " questions with no payload. CI: 95% Wilson. Groups: tune = the 15 tune cases of"
        " INJ-01..30 (defenses are tuned on these); heldout_1 = their 15 held-out cases;"
        " heldout_2 = INJ-31..60, written before any run; heldout_pooled = both held-out sets.",
        "",
        "| config | group | attack success [95% CI] | strict | exposed | clean control |",
        "|---|---|---|---|---|---|",
    ]
    for r in runs:
        for group in ("tune", "heldout_1", "heldout_2", "heldout_pooled"):
            if group not in r["summary"]:
                continue
            s = r["summary"][group]
            out.append(
                f"| {r['config']['name']} | {group} | {cell(s['attack_success'])} |"
                f" {frac(s['strict_success'])} | {frac(s['exposed'])} |"
                f" {frac(s['clean_control_success'])} |"
            )
    base = runs[0]
    out += [
        "",
        f"| slice, {base['config']['name']}, all cases run (context only) | attack success |",
        "|---|---|",
    ]
    rows = base["cases"]
    for key, names in (("location", LOCATIONS), ("style", STYLES)):
        for name in names:
            part = [x for x in rows if x[key] == name]
            out.append(f"| {name} | {sum(x['attack_success'] for x in part)}/{len(part)} |")
    return out


def placeholder_section(measured: set[str] = frozenset()) -> list[str]:
    out = ["## (d) Not measured yet", "", "| metric | status |", "|---|---|"]
    out += [
        f"| {name} | not measured yet (Phase {p}) |"
        for name, p in PLACEHOLDERS
        if name not in measured
    ]
    return out


def bullets_section() -> list[str]:
    text = GUIDE.read_text()
    block = text[text.index("**Bullet templates**") : text.index("**Skills line:**")]
    templates = [ln[2:].strip() for ln in block.splitlines() if ln.startswith("- ")]
    out = [
        "## (e) Resume bullet templates (docs/BUILD_GUIDE.md) and the rows that fill them",
        "",
        "Only test-split rows fill the brackets. A bullet whose number comes out weak is dropped.",
        "",
    ]
    for t in templates:
        rows = next((v for k, v in BULLET_ROWS.items() if t.startswith(k)), None)
        if rows is None:
            raise SystemExit(f"no ledger mapping for bullet template: {t[:60]!r}")
        out += [f"- {t}", f"  - Filled by: {rows}"]
    return out


def render() -> str:
    questions = load(QUESTIONS)
    a = corpus_section(questions)
    b = retrieval_section(questions)
    c = generation_section(questions)
    c2 = agreement_section()
    c3 = injection_section()
    out = [
        "# OpsPilot results ledger",
        "",
        "Generated by `make results` (scripts/results.py) from committed files only:"
        " eval/results/*.json, eval/questions.jsonl, data/processed and eval/label_audit.md."
        " Do not edit by hand: `make gate` fails when this file is stale.",
        "",
        *a,
        "",
        *b,
        "",
        *([*c, ""] if c else []),
        *([*c2, ""] if c2 else []),
        *([*c3, ""] if c3 else []),
        *placeholder_section(
            (GEN_MEASURED if c else frozenset())
            | ({"Judge agreement with Prince's 30 hand labels"} if c2 else frozenset())
            | (INJECTION_MEASURED if c3 else frozenset())
        ),
        "",
        *bullets_section(),
    ]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    text = render()
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print("docs/results.md is stale: run `make results` and commit it")
            return 1
        print("docs/results.md is current")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
