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
import random
import re
import sys
from collections import Counter

from config import get_settings
from eval.ablation import RESAMPLES, bootstrap
from eval.injection_eval import LOCATIONS, STYLES
from eval.retrieval_eval import NAMES, RESULTS, token_gold
from eval.validate_questions import QUESTIONS, SPLIT_DIMS, Corpus, load
from scripts.corpus.common import PROCESSED, ROOT, SEED

OUT = ROOT / "docs" / "results.md"
LIVE_CASES = ROOT / "eval" / "live_questions.jsonl"
AUDIT = ROOT / "eval" / "label_audit.md"

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
COST_MEASURED = {"Tokens per investigation (input / output)", "Estimated cost per investigation"}
DEFENDED_MEASURED = {
    "Attack success after defenses, tune half (15)",
    "Attack success after defenses, held-out half (15)",
}


def load_named(split: str, name: str) -> dict | None:
    path = RESULTS / f"{split}_{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def paired_diff(a: dict, b: dict, key: str, keep=lambda i: True) -> tuple:
    """a minus b per question (retrieval result files), over questions where both have the
    metric, in sorted id order. The one method the ledger uses for hybrid - dense: the bootstrap
    is seeded, so the order of the pairs fixes which resamples are drawn, and sorting makes the
    CI independent of a result file's row order. Returns (diff, lo, hi, n)."""
    ra = {x["id"]: x for x in a["questions"]}
    rb = {x["id"]: x for x in b["questions"]}
    ids = sorted(
        i for i in ra if i in rb and ra[i].get(key) is not None and rb[i].get(key) is not None
    )
    ids = [i for i in ids if keep(i)]
    d, lo, hi = bootstrap([ra[i][key] for i in ids], [rb[i][key] for i in ids])
    return d, lo, hi, len(ids)


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
        f" 95% CI, 10,000 resamples, seed {SEED}, pairs in sorted question-id order (the one"
        " method this ledger uses for hybrid - dense, here and in (e) and (f)). Claimable = yes"
        " only when the CI excludes 0.",
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
        for metric, key, sl in COMPARISONS:
            diff, lo, hi, n = paired_diff(
                a, b, key, lambda i, sl=sl, by_id=by_id: sl in (None, by_id[i]["difficulty"])
            )
            claim = "yes" if lo > 0 or hi < 0 else "no"
            where = "" if sl is None else f", {sl} only"
            out.append(
                f"| {NAMES[metric]}: locked - dense{where} | difference of the levels above"
                f" | {split} | {locked} vs dense | {n} | {diff:+.3f}"
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


INJECTION_CONFIGS = (
    "no_defense",
    "d1",
    "d1_d2",
    "d1_d2_d3_withhold",
    "d1_d2_d3_warn",
)  # in the order they are reported


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
        "| config | group | attack success [95% CI] | strict | exposed | success given exposed |"
        " clean control |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        for group in ("tune", "heldout_1", "heldout_2", "heldout_pooled"):
            if group not in r["summary"]:
                continue
            s = r["summary"][group]
            out.append(
                f"| {r['config']['name']} | {group} | {cell(s['attack_success'])} |"
                f" {frac(s['strict_success'])} | {frac(s['exposed'])} |"
                f" {frac(s['success_given_exposed'])} | {frac(s['clean_control_success'])} |"
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
    return out + ([""] + extra if (extra := defense_sections()) else [])


def defense_sections() -> list[str]:
    """Phase 4 defenses: the paired before/after, false alarms, utility cost (each if run)."""
    out: list[str] = []
    cmp_path = RESULTS / "injection_comparison.json"
    if cmp_path.exists():
        c = json.loads(cmp_path.read_text())
        out += [
            f"### Before / after: {c['before']} vs {c['after']} (same cases, paired)",
            "",
            f"Defenses frozen at commit {c['frozen_commit'][:12]}; held-out run once. Tune was"
            " tuned on, so its after is optimistic. Exact McNemar: b = success before only,"
            " c = success after only, two-sided.",
            "",
            "| group | n | before [95% CI] | after [95% CI] | strict before / after | exposed"
            " before / after | given exposed before / after | clean control after | McNemar b, c,"
            " p |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for g in ("tune", "heldout_1", "heldout_2", "heldout_pooled"):
            if g not in c["groups"]:
                continue
            x = c["groups"][g]
            m = x["mcnemar"]

            def ci(r):
                return f"{r['k']}/{r['n']} [{r['ci95'][0]:.3f}, {r['ci95'][1]:.3f}]"

            def kn(r):
                return f"{r['k']}/{r['n']}" if r else "-"

            label = f"{g} (tuned on; optimistic)" if g == "tune" else g
            out.append(
                f"| {label} | {x['n']} | {ci(x['before'])} | {ci(x['after'])} |"
                f" {kn(x['strict_before'])} / {kn(x['strict_after'])} |"
                f" {kn(x['exposed_before'])} / {kn(x['exposed_after'])} |"
                f" {kn(x['given_exposed_before'])} / {kn(x['given_exposed_after'])} |"
                f" {kn(x['clean_after'])} | {m['b_before_only']}, {m['c_after_only']},"
                f" {m['p_exact']:.4f} |"
            )
        out += [
            "",
            "Exposed in both runs only (a defended agent reads differently, and an unseen payload"
            " cannot succeed; this isolates resistance to payloads it saw):",
            "",
            "| group | n | before | after | McNemar b, c, p |",
            "|---|---|---|---|---|",
        ]
        for g in ("tune", "heldout_1", "heldout_2", "heldout_pooled"):
            if g not in c["groups"]:
                continue
            e = c["groups"][g]["exposed_both"]
            m = e["mcnemar"]
            out.append(
                f"| {g} | {e['n']} | {e['before']['k']}/{e['n']} | {e['after']['k']}/{e['n']} |"
                f" {m['b_before_only']}, {m['c_after_only']}, {m['p_exact']:.4f} |"
            )
        out.append("")
    fa_path = RESULTS / "injection_false_alarms.json"
    if fa_path.exists():
        f = json.loads(fa_path.read_text())
        out += [
            "### False alarms on clean data, and detection on the 60 payload carriers",
            "",
            f"D2 = instruction-like text flagged; D3 = `{f['guard_model']}` score >="
            f" {f['guard_threshold']}. Clean: every main-corpus chunk and {f['log_sample']['n']}"
            f" log lines sampled with seed {f['log_sample']['seed']}.",
            "",
            "| set | kind | n | D2 flagged | D3 flagged | either |",
            "|---|---|---|---|---|---|",
        ]
        for setname in ("clean", "payloads"):
            for kind, r in f[setname].items():
                pct = {k: f"{r[k]} ({r[k] / r['n']:.1%})" for k in ("d2", "d3", "either")}
                out.append(
                    f"| {setname} | {kind} | {r['n']} | {pct['d2']} | {pct['d3']} |"
                    f" {pct['either']} |"
                )
        out.append("")
    dc_path = RESULTS / "dev_generation_defense_cost.json"
    if dc_path.exists():
        d = json.loads(dc_path.read_text())
        out += [
            "### Utility cost of the defenses (dev, verify on, no payloads)",
            "",
            f"Defended ({d['defenses']}) minus the locked config, paired by question, bootstrap"
            f" 95% CI (seed {d['seed']}); * = the CI excludes 0. Measurement only: nothing was"
            " tuned on it.",
            "",
            "| metric | n | locked | defended | defended - locked [95% CI] |",
            "|---|---|---|---|---|",
        ]
        for r in d["rows"]:
            if r.get("not_measured"):
                out.append(f"| {r['metric']} | {r['n']} | - | - | not measured (n {r['n']}) |")
                continue
            star = " *" if r["lo"] > 0 or r["hi"] < 0 else ""
            out.append(
                f"| {r['metric']} | {r['n']} | {r['locked']:.4f} | {r['defended']:.4f} |"
                f" {r['diff']:+.4f} [{r['lo']:+.4f}, {r['hi']:+.4f}]{star} |"
            )
        out.append("")
    return out


def shipped_section() -> list[str]:
    """Shipped vs locked (eval/results/dev_generation_shipped_vs_locked.json)."""
    path = RESULTS / "dev_generation_shipped_vs_locked.json"
    if not path.exists():
        return []
    d = json.loads(path.read_text())
    out = [
        "## (c6) The shipped agent (dev, verify on, no payloads)",
        "",
        f"The API's agent `{d['shipped']}` (verify on, live-only first look; agent/run.py"
        " SHIPPED) minus the locked config and minus the defended config (which differs only in"
        f" first look), paired by question, bootstrap 95% CI (seed {d['seed']}); * = the CI"
        " excludes 0. Deterministic metrics only, measurement only. First look fired on the"
        f" {len(d['live_triaged'])} questions triaged live: {', '.join(d['live_triaged'])}.",
        "",
        "| metric | n | locked | shipped | shipped - locked [95% CI] | defended |"
        " shipped - defended [95% CI] |",
        "|---|---|---|---|---|---|---|",
    ]
    for r, q in zip(d["vs"]["verify_on"], d["vs"]["defended_on"], strict=True):
        star = lambda x: " *" if x["lo"] > 0 or x["hi"] < 0 else ""  # noqa: E731
        out.append(
            f"| {r['metric']} | {r['n']} | {r['b']:.4f} | {r['a']:.4f} |"
            f" {r['diff']:+.4f} [{r['lo']:+.4f}, {r['hi']:+.4f}]{star(r)} | {q['b']:.4f} |"
            f" {q['diff']:+.4f} [{q['lo']:+.4f}, {q['hi']:+.4f}]{star(q)} |"
        )
    out.append("")
    return out


def cost_section() -> list[str]:
    """Cost, tokens and cache hits per generation run (eval/results/dev_generation_cost.json)."""
    path = RESULTS / "dev_generation_cost.json"
    if not path.exists():
        return []
    d = json.loads(path.read_text())
    out = [
        "## (c4) Cost and tokens (generation runs)",
        "",
        f"From `eval/results/{path.name}` (`uv run python -m eval.generation_eval cost`), built"
        " from the same per-question rows as each config's result file. One row per run of the"
        f" agent over the {d['split']} split; the judge's calls are not included (Gemini free"
        " tier). Tokens and $ per investigation are means over its LLM calls as made: a cache"
        " hit keeps the original call's tokens and cost, so they describe one investigation run"
        " fresh. Run spend is what the run actually paid (its calls that were not cache hits),"
        " from the result file. Cache hit rate is LLM response-cache hits / LLM calls, as"
        " recorded when each question ran; an _off config replays its _on run, so it is all"
        " hits by design.",
        "",
        "| config | n | input tokens / inv. | output tokens / inv. | LLM calls / inv."
        " | $ / inv. (as made) | run spend $ | cache hits | cache hit rate | result file commit |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in d["runs"]:
        dirty = r["result_uncommitted"]
        out.append(
            f"| {r['config']} | {r['n']} | {r['prompt_tokens_mean']:,.0f} |"
            f" {r['completion_tokens_mean']:,.0f} | {r['llm_calls_mean']:.1f} |"
            f" {r['cost_usd_mean']:.4f} | {r['spend_usd']:.4f} | {r['cache_hits']} |"
            f" {r['cache_hit_rate']:.3f} | {r['result_commit'][:12]}"
            + (f" + {dirty} uncommitted file(s)" if dirty else "")
            + " |"
        )
    total = sum(r["spend_usd"] for r in d["runs"])
    out += ["", f"Total run spend over these {len(d['runs'])} runs: ${total:.4f}."]
    return out


def live_section() -> list[str]:
    """Phase 4 live-incident set (eval/results/live_incidents.json), deterministic metrics only."""
    path = RESULTS / "live_incidents.json"
    if not path.exists():
        return []
    d = json.loads(path.read_text())
    cases = {json.loads(x)["id"]: json.loads(x) for x in LIVE_CASES.read_text().splitlines()}
    names = {"defended": "first look off (locked)", "defended_first_look": "first look on"}
    out = [
        "## (c5) Live incidents (eval/live_questions.jsonl, n = 10)",
        "",
        f"`eval/results/{path.name}` ({provenance(d)}). Ten on-call questions whose answers exist"
        " only in each case's own logs (data/live/), answer keys committed before any run. The"
        " locked defended agent (verify on, frozen defenses) with first look off, then on."
        " Deterministic metrics, no judge. Key facts: exact-token match of the key's tokens"
        " in the summary, likely cause and kept claims. Forbidden claims: the key's regex"
        " matches a sentence with no negation cue. Gold coverage: gold log entries with a row"
        " among the gathered evidence. Post hoc (after the first run, not pre-registered): the"
        " same answers with Unicode hyphens and spaces folded to ASCII, because the model writes"
        " U+2011 in codes and host names.",
        "",
        "| config | key facts (pre-registered) | key facts (post hoc, ASCII) | cases with every"
        " key fact | cases with a forbidden claim | gold entries covered | tool calls (failed)"
        " | triaged live | run spend $ |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, c in d["configs"].items():
        s, p = c["summary"], c["posthoc_ascii"]
        out.append(
            f"| {names[name]} | {s['key_facts_found']}/{s['key_facts_total']} |"
            f" {p['key_facts_found']}/{s['key_facts_total']} | {s['cases_all_key_facts']}/{s['n']}"
            f" (post hoc {p['cases_all_key_facts']}) | {s['cases_with_forbidden_claim']}/{s['n']} |"
            f" {s['gold_covered']}/{s['gold_total']} | {s['tool_calls']} ({s['tool_failures']}) |"
            f" {s['triaged_live']}/{s['n']} | {s['spend_usd']:.4f} |"
        )
    out += [
        "",
        "| case | cause type | key facts off / on | post hoc off / on | gold off / on"
        " | tool calls off / on |",
        "|---|---|---|---|---|---|",
    ]
    off, on = (d["configs"][k] for k in ("defended", "defended_first_look"))
    for a, b in zip(off["per_case"], on["per_case"], strict=True):
        i = a["id"]
        pa, pb = off["posthoc_ascii"]["key_facts"][i], on["posthoc_ascii"]["key_facts"][i]
        out.append(
            f"| {i} | {cases[i]['cause_type']} | {a['key_facts_found']} / {b['key_facts_found']}"
            f" (of {a['key_facts_total']}) | {pa} / {pb} |"
            f" {a['gold_covered']} / {b['gold_covered']} (of {a['gold_total']}) |"
            f" {a['tool_calls']} / {b['tool_calls']} |"
        )
    hits = [
        f'{names[n]}, {r["id"]}: {claim}: "{sent}"'
        for n, c in d["configs"].items()
        for r in c["per_case"]
        for claim, sents in r["forbidden_hits"].items()
        for sent in sents
    ]
    out += ["", "Forbidden-claim hits, for audit: " + ("; ".join(hits) if hits else "none") + "."]
    for label, key in (("pre-registered", "comparison"), ("post hoc", "comparison_posthoc_ascii")):
        m = d[key]
        out.append(
            f"First look on minus off ({label}), paired by case, n = {m['n']}: key facts better in"
            f" {m['key_facts_better']}, worse in {m['key_facts_worse']}, tied in"
            f" {m['key_facts_tied']} (exact sign test p = {m['key_facts_sign_p']}); every key fact"
            f" in {m['all_key_facts_only_first']} case(s) only with first look off and"
            f" {m['all_key_facts_only_second']} only with it on (exact McNemar p ="
            f" {m['all_key_facts_mcnemar_p']}); gold entries covered {m['gold_covered_diff']:+d};"
            f" tool calls {m['tool_calls_diff']:+d}."
        )
    p = d["comparison"]["key_facts_sign_p"]
    out.append(
        ""
        if p < 0.05
        else f"With n = 10 the two configs cannot be separated (sign test p = {p} > 0.05)."
        " These are descriptive numbers, not a claim that first look helps or hurts."
    )
    return out


STATUS = {
    "p50 / p95 latency, end to end per investigation": "end-to-end latency per investigation:"
    " not measured; the test run was fully cached. Phase 5B measures it on EC2 with the cache"
    " disabled on 20 dev questions",
}


def placeholder_section(measured: set[str] = frozenset()) -> list[str]:
    out = ["## (d) Not measured yet", "", "| metric | status |", "|---|---|"]
    out += [
        f"| {name} | {STATUS.get(name, f'not measured yet (Phase {p})')} |"
        for name, p in PLACEHOLDERS
        if name not in measured
    ]
    return out


def empty_final_notes(on: dict, off: dict) -> list[str]:
    """One line per question whose final answer has no claims, from the committed rows: the
    first draft (the verify-off row of the same trajectory) and what verify left."""
    first = {q["id"]: q for q in off["questions"]}
    out = []
    for q in sorted(on["questions"], key=lambda q: q["id"]):
        if q["claims"]:
            continue
        f = first[q["id"]]
        ok, n = f["scores"]["citation_a"] or [0, 0]
        fell_back = q["scores"]["no_diagnosis"][0] == 1
        out.append(
            f"  - {q['id']}: first draft {n} claims ({ok} passed the check); verify sent it back"
            f" {q['loops']} times and each pass redrafted; the last draft had"
            f" {q['claims'] + q['dropped_claims']} claim(s), {q['dropped_claims']} failed and"
            f" {'was' if q['dropped_claims'] == 1 else 'were'} dropped, so the final answer has"
            " an empty claims list"
            + (
                " (the no-diagnosis fallback)."
                if fell_back
                else " (not the no-diagnosis fallback)."
            )
        )
    return out


def bullets_section() -> list[str]:
    """The resume bullets, filled from the test-split rows of this ledger (and the held-out
    injection and judge-agreement files), each with its source row and CI."""
    from eval.generation_eval import paired_bootstrap

    out = [
        "## (e) Resume bullets (test split only)",
        "",
        "Every number comes from the files named under each bullet; nothing is typed by hand."
        " Wording was set after the test run (Phase 5A), so each bullet says which"
        " pre-registered row it uses; disclosures below a bullet must go wherever it is used.",
        "",
    ]
    a, b = load_named("test", "locked"), load_named("test", "dense_baseline")
    on, off = load_named("test", "generation_shipped"), load_named("test", "generation_shipped_off")
    if not (a and b and on and off):
        return [*out, "Not filled: the test-split result files do not exist yet."]

    d, lo, hi, n = paired_diff(a, b, "rr")
    r5, r5lo, r5hi, _ = paired_diff(a, b, "recall@5")
    r10, r10lo, r10hi, _ = paired_diff(a, b, "recall@10")
    mh, md = a["metrics"]["overall"]["mrr"], b["metrics"]["overall"]["mrr"]
    h10, d10 = a["metrics"]["overall"]["recall@10"], b["metrics"]["overall"]["recall@10"]
    docs = sum(a["corpus"]["documents"].values())
    out += [
        f"- Built hybrid BM25 + dense retrieval over {docs} documents (pgvector + Postgres"
        f" full-text); on a {n}-question held-out set it raised MRR from {md:.3f} to"
        f" {mh:.3f} ({d:+.3f}, 95% CI [{lo:+.3f}, {hi:+.3f}]).",
        f"  - Optional second sentence: Recall@10 rose from {d10:.3f} to {h10:.3f} ({r10:+.3f},"
        f" 95% CI [{r10lo:+.3f}, {r10hi:+.3f}]).",
        "  - Source: section (f) retrieval, MRR and Recall@10 rows (test, hybrid and dense) and"
        ' "hybrid - dense, paired"; test_locked.json, test_dense_baseline.json.',
        f"  - Disclose (README): the Recall@5 gain did not replicate on test, {r5:+.3f}"
        f" [{r5lo:+.3f}, {r5hi:+.3f}]. MRR was in the pre-registered metric list.",
    ]

    first = level_ci(gen_pairs(off, "citation_a"))
    final = level_ci(gen_pairs(on, "citation_a"))
    tools = level_ci(gen_pairs(on, "tool_success"))
    cost = level_ci(gen_pairs(on, "cost"))
    qa = {q["id"]: q["scores"]["citation_a"] for q in on["questions"]}
    qo = {q["id"]: q["scores"]["citation_a"] for q in off["questions"]}
    pids = sorted(i for i in qa if qa[i] and qo.get(i))
    vd, vlo, vhi = paired_bootstrap(qa, qo, pids)
    out += [
        "- Built a LangGraph incident-investigation agent with 5 read-only tools and a"
        f" citation-verification loop: {1 - first[0]:.1%} of first-draft claims failed a"
        " deterministic evidence check and the loop removed all of them"
        f" ({len(pids)} held-out questions), at {tools[0]:.1%} tool-call success and about"
        f" ${cost[0]:.4f} per investigation.",
        "  - Source: section (f) generation, test column: first-draft citation (a) failure"
        f" {1 - first[0]:.3f} [{1 - first[2]:.3f}, {1 - first[1]:.3f}]; final citation (a)"
        f" {final[0]:.3f}; verify on - off {vd:+.3f} [{vlo:+.3f}, {vhi:+.3f}] (n {len(pids)});"
        f" tool-call success {tools[0]:.3f} [{tools[1]:.3f}, {tools[2]:.3f}]; $ per"
        f" investigation {cost[0]:.4f} [{cost[1]:.4f}, {cost[2]:.4f}].",
        "  - Disclose: final citation (a) is what the verify node enforces, so it is 1.0 by"
        " construction; the claim is the first-draft failure rate.",
        f"  - Note: first-draft failure rate is over {len(off['questions'])} questions;"
        f" {len(pids)} had claims in the final answer; verify drops failing claims, it does not"
        " repair them.",
        *empty_final_notes(on, off),
        "  - Disclose: verify checks claims, not the summary or likely-cause text, so a dropped"
        " claim can still be asserted in the summary (seen in Q030).",
        "  - Disclose: tool-call success counts repeated calls as failures (the same tool with"
        " the same arguments, or re-reading a document part already in the evidence: refused,"
        " not run), so it is not the share of calls that hit an error.",
    ]

    g = json.loads((RESULTS / "injection_comparison.json").read_text())["groups"]["heldout_pooled"]
    m, e = g["mcnemar"], g["exposed_both"]
    out += [
        f"- Measured prompt-injection attack success on {g['n']} held-out cases:"
        f" {g['before']['k']}/{g['n']} undefended vs {g['after']['k']}/{g['n']} with layered"
        f" defenses ({g['strict_after']['k']} strict hits; McNemar p = {m['p_exact']:.3f}).",
        "  - Source: section (c3), heldout_pooled row; before"
        f" [{g['before']['ci95'][0]:.3f}, {g['before']['ci95'][1]:.3f}], after"
        f" [{g['after']['ci95'][0]:.3f}, {g['after']['ci95'][1]:.3f}] (Wilson 95%).",
        f"  - Disclose: exposure-matched (payload read in both runs, n {e['n']}):"
        f" {e['before']['k']}/{e['n']} -> {e['after']['k']}/{e['n']}, McNemar p ="
        f" {e['mcnemar']['p_exact']:.3f}, not significant. The Phase 5 figure is a cache"
        " replay of the Phase 4 run on the frozen code (every call a cache hit), not a fresh"
        " sample.",
        "  - Disclose: these numbers are from the `defended` agent (locked config + the frozen"
        " defenses, first look off), not exactly the shipped agent (which adds first look on"
        " live incidents; the injection cases are past incidents).",
    ]

    k = json.loads((RESULTS / "judge_agreement.json").read_text())["overall"]
    kf, kc = k["key_facts"], k["claims"]
    out += [
        "- Validated an LLM judge against 30 hand-labelled answers (Cohen's kappa"
        f" {kf['kappa']:.2f} on key facts, {kc['kappa']:.2f} on claim support) and based"
        " decisions on deterministic metrics.",
        f"  - Source: section (c2); key facts kappa {kf['kappa']:.3f} [{kf['kappa_ci95'][0]:.3f},"
        f" {kf['kappa_ci95'][1]:.3f}] (n {kf['n']}); claims {kc['kappa']:.3f}"
        f" [{kc['kappa_ci95'][0]:.3f}, {kc['kappa_ci95'][1]:.3f}] (n {kc['n']}).",
        "- Deploy (FastAPI, Postgres and the dashboard on AWS EC2, p95 latency): pending"
        " Phase 5B (not measured).",
    ]
    return out


def level_ci(pairs: list) -> tuple[float, float, float] | None:
    """Pooled ratio of per-question [num, den] pairs and its 95% percentile-bootstrap CI
    (resampling questions, seed SEED). None when no question has the metric."""
    from eval.generation_eval import ratio

    pairs = [p for p in pairs if p is not None and p[1]]
    if not pairs:
        return None
    rng = random.Random(SEED)
    stats = sorted(ratio(rng.choices(pairs, k=len(pairs))) for _ in range(RESAMPLES))
    return ratio(pairs), stats[int(0.025 * RESAMPLES)], stats[int(0.975 * RESAMPLES) - 1]


def cell(pairs: list, invert: bool = False, digits: int = 3) -> str:
    """value [95% CI] (n questions); invert reports 1 - the rate (a failure rate)."""
    c = level_ci(pairs)
    if c is None:
        return "- (n 0)"
    v, lo, hi = (1 - c[0], 1 - c[2], 1 - c[1]) if invert else c
    n = sum(1 for p in pairs if p is not None and p[1])
    return f"{v:.{digits}f} [{lo:.{digits}f}, {hi:.{digits}f}] (n {n})"


RET_TEST = [  # (label, per-question key)
    ("Recall@5", "recall@5"),
    ("Recall@10", "recall@10"),
    ("MRR", "rr"),
    ("all-gold Recall@10 (2+ gold docs)", "all_gold@10"),
    ("Hit@5", "hit@5"),
]
# (label, config, per-question metric, invert): the pre-registered generation metrics (NOTES,
# "Phase 5 pre-registration"). Judged citation (b) and faithfulness are not reported.
GEN_TEST = [
    ("citation accuracy (a)", "shipped", "citation_a", False),
    ("first-draft citation (a) failure (verify off)", "shipped_off", "citation_a", True),
    ("gold_all: gathered every gold doc", "shipped", "gold_all", False),
    ("gold_frac: share of gold docs gathered", "shipped", "gold_frac", False),
    ("tool-call success", "shipped", "tool_success", False),
    ("verify-loop rate", "shipped", "verify_loop", False),
    ("no diagnosis (fallback returned)", "shipped", "no_diagnosis", False),
    ("completeness (judged; see kappa note)", "shipped", "completeness", False),
]
GEN_SLICE_COLS = ["citation_a", "gold_all", "gold_frac", "tool_success", "completeness"]


def gen_pairs(r: dict | None, metric: str, ids: set | None = None) -> list:
    if r is None:
        return []
    rows = [q for q in r["questions"] if ids is None or q["id"] in ids]
    if metric == "cost":
        return [[q["cost_usd"], 1] for q in rows]
    return [q["scores"][metric] for q in rows]


def test_section(questions: list[dict]) -> list[str]:
    """Phase 5: the one test-split run beside dev, every level with its bootstrap CI."""
    ret = {sp: load_named(sp, "locked") for sp in ("dev", "test")}
    dense = {sp: load_named(sp, "dense_baseline") for sp in ("dev", "test")}
    gen = {
        (sp, c): load_named(sp, f"generation_{c}")
        for sp in ("dev", "test")
        for c in ("shipped", "shipped_off")
    }
    if not ret["test"] and not gen[("test", "shipped")]:
        return []
    by_id = {q["id"]: q for q in questions}
    kappa = json.loads((RESULTS / "judge_agreement.json").read_text())["overall"]["key_facts"]
    out = [
        "## (f) Test split: the one pre-registered run (Phase 5)",
        "",
        'Pre-registered in NOTES.md ("Phase 5 pre-registration") before any test question ran;'
        " code frozen at tag `v0.1-frozen`; run once with `--final-test`. Every cell is value"
        f" [95% CI] (n questions); CI: percentile bootstrap resampling questions, {RESAMPLES:,}"
        f" resamples, seed {SEED}; rates pool claims, calls or key facts over the questions"
        " resampled. **Retrieval and agent settings were tuned on dev, the injection defenses on"
        " 15 separate tune cases, and first look was chosen after the 10-case live-incident set;"
        " the test split was not used for any of these. So dev numbers are optimistic and a drop"
        " from dev to test is expected; only the test column is a result.**",
        "",
        "### Retrieval (locked: hybrid BM25 + dense, RRF; baseline: dense)",
        "",
        "| metric | config | dev | test |",
        "|---|---|---|---|",
    ]
    for name, src in (("hybrid (locked)", ret), ("dense", dense)):
        for label, key in RET_TEST:
            cells = [
                cell([[q[key], 1] if q.get(key) is not None else None for q in r["questions"]])
                if r
                else "not run"
                for r in (src["dev"], src["test"])
            ]
            out.append(f"| {label} | {name} | {cells[0]} | {cells[1]} |")
    out += [
        "",
        "| hybrid - dense, paired (same method as (b): sorted ids) | dev | test |",
        "|---|---|---|",
    ]
    for label, key in RET_TEST[:3]:
        cells = []
        for sp in ("dev", "test"):
            a, b = ret[sp], dense[sp]
            if not a or not b:
                cells.append("not run")
                continue
            d, lo, hi, n = paired_diff(a, b, key)
            cells.append(f"{d:+.3f} [{lo:+.3f}, {hi:+.3f}] (n {n})")
        out.append(f"| {label} | {cells[0]} | {cells[1]} |")
    out += [
        "",
        "Per slice, hybrid (locked):",
        "",
        "| slice | metric | dev | test |",
        "|---|---|---|---|",
    ]
    for dim in ("difficulty", "author"):
        for v in sorted({q[dim] for q in questions}):
            for label, key in RET_TEST[:3]:
                cells = [
                    cell([[x[key], 1] for x in r["questions"] if by_id[x["id"]][dim] == v])
                    if r
                    else "not run"
                    for r in (ret["dev"], ret["test"])
                ]
                out.append(f"| {dim}={v} | {label} | {cells[0]} | {cells[1]} |")

    out += [
        "",
        "### Generation (the shipped agent: locked + defenses on + live-only first look)",
        "",
        "Verify off is the same trajectory's first draft (cached replay), so on vs off is paired."
        f" Completeness is judged by the Gemini judge, whose key-fact agreement with Prince's"
        f" hand labels is kappa {kappa['kappa']:.2f} [{kappa['kappa_ci95'][0]:.2f},"
        f" {kappa['kappa_ci95'][1]:.2f}] (n {kappa['n']} facts): moderate, so completeness is"
        " context, not a headline. Judged citation (b) is not reported (judge kappa -0.05)."
        " Dev shipped was not judged (its completeness cell is empty).",
        "",
        "| metric | dev | test |",
        "|---|---|---|",
    ]
    for label, config, m, inv in GEN_TEST:
        cells = [
            cell(gen_pairs(gen[(sp, config)], m), inv) if gen[(sp, config)] else "not run"
            for sp in ("dev", "test")
        ]
        out.append(f"| {label} | {cells[0]} | {cells[1]} |")
    cost = [
        cell(gen_pairs(gen[(sp, "shipped")], "cost"), digits=4) if gen[(sp, "shipped")] else "-"
        for sp in ("dev", "test")
    ]
    out.append(f"| $ per investigation (as made) | {cost[0]} | {cost[1]} |")
    for k, unit in (("end_to_end_s", "s"), ("llm_call_ms", "ms")):
        cells = []
        for sp in ("dev", "test"):
            r = gen[(sp, "shipped")]
            lat = r["latency"][k] if r else None
            cells.append(
                f"p50 {lat['p50']:.1f} {unit}, p95 {lat['p95']:.1f} {unit} (n {lat['n']})"
                if lat and lat["n"]
                else "not measured (cache hits)"
            )
        out.append(f"| latency {k} (live calls only) | {cells[0]} | {cells[1]} |")
    out += ["", "| verify on - off, citation (a), paired | dev | test |", "|---|---|---|"]
    cells = []
    for sp in ("dev", "test"):
        on, off = gen[(sp, "shipped")], gen[(sp, "shipped_off")]
        if not on or not off:
            cells.append("not run")
            continue
        from eval.generation_eval import paired_bootstrap

        qa = {q["id"]: q["scores"]["citation_a"] for q in on["questions"]}
        qb = {q["id"]: q["scores"]["citation_a"] for q in off["questions"]}
        ids = sorted(i for i in qa if qa[i] and qb.get(i))
        d, lo, hi = paired_bootstrap(qa, qb, ids)
        cells.append(f"{d:+.3f} [{lo:+.3f}, {hi:+.3f}] (n {len(ids)})")
    out.append(f"| overall | {cells[0]} | {cells[1]} |")
    for sp in ("test", "dev"):
        r = gen[(sp, "shipped")]
        if not r:
            continue
        out += [
            "",
            f"Per slice, shipped, {sp}:",
            "",
            "| slice | " + " | ".join(GEN_SLICE_COLS) + " |",
            "|---|" + "---|" * len(GEN_SLICE_COLS),
        ]
        for dim in ("difficulty", "author"):
            for v in sorted({q[dim] for q in questions}):
                ids = {q["id"] for q in questions if q[dim] == v and q["split"] == sp}
                cells = [cell(gen_pairs(r, m, ids)) for m in GEN_SLICE_COLS]
                out.append(f"| {dim}={v} | " + " | ".join(cells) + " |")
    spend = sum((r or {}).get("spend_usd") or 0.0 for (sp, _), r in gen.items() if sp == "test")
    if gen[("test", "shipped")]:
        out += ["", f"Test-run agent spend (new, uncached LLM calls): ${spend:.4f}."]
    return out


# Phase 5B, from `docker image inspect` on the server (image ID only) and GHCR's commit-SHA tag;
# the app image's digest is also in eval/results/deploy_latency.json.
PROXY_DIGEST = "sha256:509d33f827ba90552ae1cc10e380c9b58e4cafe8d63833ac1ee3c5ed8836e24e"


def deploy_section() -> list[str]:
    """Phase 5B end-to-end latency on EC2 (eval/results/deploy_latency.json)."""
    path = RESULTS / "deploy_latency.json"
    if not path.exists():
        return []
    d = json.loads(path.read_text())
    runs = d["runs"]
    walls = sorted(r["wall_s"] for r in runs)
    c = d["cost_usd"]
    out = [
        "## (g) Deployed latency (EC2, Phase 5B, dev)",
        "",
        f"`eval/results/{path.name}` (scripts/deploy_latency.py). n = {d['n']} dev questions,"
        f" a seeded sample (seed {d['seed']}): {', '.join(d['question_ids'])}. Shipped config,"
        f" the server's LLM response cache off ({d['cached_llm_calls']} cached calls),"
        f" as_of {d['as_of']}. Image built from commit {d['commit'][:7]}: app"
        f" `{d['app_image_digest']}`, proxy `{PROXY_DIGEST}`. One sequential client on a Mac in"
        " Calgary, plain HTTP through Nginx to one t3.medium in ca-central-1. Wall time: from"
        " just before the POST until the server closes the SSE stream.",
        "",
        "| metric | n | value |",
        "|---|---|---|",
        f"| wall time p50 | {d['n']} | {d['wall_s']['p50']:.1f} s |",
        f"| wall time p95 | {d['n']} | {d['wall_s']['p95']:.1f} s |",
        f"| first SSE event p50 | {d['n']} | {d['first_event_s']['p50']:.2f} s |",
        f"| errors (not HTTP 200 and status done) | {d['n']} | {d['errors']} |",
        f"| cost per investigation, mean | {c['n']} | ${c['mean']:.5f} |",
        f"| cost, total | {c['n']} | ${c['total']:.4f} |",
        "",
        "| tool calls | runs | wall min | wall max |",
        "|---|---|---|---|",
    ]
    for k in sorted({r["tool_calls"] for r in runs}):
        w = [r["wall_s"] for r in runs if r["tool_calls"] == k]
        out.append(f"| {k} | {len(w)} | {min(w):.1f} s | {max(w):.1f} s |")
    out += [
        "",
        f"- p95 over n = {d['n']} (inclusive method) lies between the two slowest runs"
        f" ({walls[-2]:.1f} s and {walls[-1]:.1f} s), so it is essentially the second-slowest"
        " run, not a stable tail estimate.",
        "- Latency is bimodal and follows the tool-call count: runs that stop after the 2"
        " first-look calls are fast, runs that use all 8 allowed calls are slow.",
        "- Not tested: concurrency (one request at a time), cold start (the app was warm),"
        " accuracy (no scoring; the questions only drive the load).",
    ]
    return out


def render() -> str:
    questions = load(QUESTIONS)
    a = corpus_section(questions)
    b = retrieval_section(questions)
    c = generation_section(questions)
    c2 = agreement_section()
    c3 = injection_section()
    c4 = cost_section()
    c5 = live_section()
    c6 = shipped_section()
    f = test_section(questions)
    g = deploy_section()
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
        *([*c4, ""] if c4 else []),
        *([*c5, ""] if c5 else []),
        *([*c6, ""] if c6 else []),
        *([*f, ""] if f else []),
        *([*g, ""] if g else []),
        *placeholder_section(
            (GEN_MEASURED if c else frozenset())
            | ({"Judge agreement with Prince's 30 hand labels"} if c2 else frozenset())
            | (INJECTION_MEASURED if c3 else frozenset())
            | (COST_MEASURED if c4 else frozenset())
            | ({"p50 / p95 latency, end to end per investigation"} if g else frozenset())
            | (
                DEFENDED_MEASURED
                if (RESULTS / "injection_comparison.json").exists()
                else frozenset()
            )
        ),
        "",
        *bullets_section(),
    ]
    # A displayed zero carries no sign: -0.0 (stored Wilson bounds) and tiny negatives that
    # round to zero would otherwise print as "-0.000". Claimable flags use the raw values.
    return re.sub(r"(?<![\w.])-(0\.0+)(?![\d])", r"\1", "\n".join(out)) + "\n"


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
