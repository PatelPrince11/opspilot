"""Generation eval (BUILD_GUIDE Phase 3 step 5): run the agent on every dev question, score it.

    make gen-eval                                  # verify on, then off; resumable
    make gen-eval ARGS="--config verify_on --concurrency 3 --max-spend 4.5"
    make judge-sheet                               # eval/judge_validation.md (30 answers)
    make judge-agreement                           # once Prince has filled the sheet

Stages, each resumable from a progress file in .cache/gen_eval/ (one JSON line per question):
1. agent: the graph on each dev question at AS_OF (after every corpus incident, so any past
   window is reachable), VERIFY_ENABLED on or off, EVAL_CONCURRENCY at a time, one Client per
   run. The LLM response cache makes a rerun free; new spend (uncached calls' reported cost) is
   summed over both configs' progress files and no run starts once it reaches --max-spend.
2. judge: one call per answer (eval/judge.py), rate limited for Gemini's free tier.
3. score and write eval/results/dev_generation_<config>.json (reproducibility as retrieval:
   commit, uncommitted files, config, models; unchanged content leaves the file alone).

Metrics are kept per question as (numerator, denominator) and summed per slice, so a rate is
over claims (or key facts, or calls) and n questions is reported beside it:
- citation_a: claims whose every cited id was gathered and whose quote is in a cited item
  (deterministic, the same check verify makes; with verify off nothing enforced it).
- citation_b: claims the judge marks supported by the evidence they cite.
- faithfulness (judge, 1-5, mean) and completeness (key facts the judge marks present).
- premise_corrected (false_premise questions), unrecorded_respected (questions with an
  unrecorded part), tool_success, verify_loop (runs that looped), no_diagnosis (the draft
  failed twice and the fallback was returned).
Answers the judge could not grade (invalid twice, or a daily limit) count for the deterministic
metrics only and are listed.
"""

import argparse
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import quantiles

import psycopg

from agent import tools
from agent.graph import MAX_LOOPS, build_graph, initial_state, quote_found
from config import get_settings
from eval.ablation import RESAMPLES
from eval.judge import JUDGE_RPD, JUDGE_RPM, PROMPT_VERSION, JudgeClient, RateLimiter, judge
from eval.retrieval_eval import RESULTS, check_split, provenance, write_named
from eval.validate_questions import QUESTIONS, load
from llm.client import Client, LLMError
from scripts.corpus.common import ROOT, SEED

AS_OF = "2026-10-06T14:52:00Z"  # the demo's as_of; every corpus incident is before it
CONFIGS = {"verify_on": True, "verify_off": False}
PROGRESS = ROOT / ".cache" / "gen_eval"
SHEET = ROOT / "eval" / "judge_validation.md"
SHEET_N = 30
NO_DIAGNOSIS = "No valid diagnosis was produced."
METRICS = (
    "citation_a",
    "citation_b",
    "faithfulness",
    "completeness",
    "premise_corrected",
    "unrecorded_respected",
    "tool_success",
    "verify_loop",
    "no_diagnosis",
)
SLICES = {
    "difficulty": lambda q: q["difficulty"],
    "author": lambda q: q["author"],
    "false_premise": lambda q: bool(q.get("false_premise")),
}


# ---------------------------------------------------------------------- progress files


def read_jsonl(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {r["id"]: r for r in rows}


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def runs_path(split: str, config: str) -> Path:
    return PROGRESS / f"{split}_{config}_runs.jsonl"


def judge_path(split: str, config: str) -> Path:
    return PROGRESS / f"{split}_{config}_judge.jsonl"


def new_spend(rows) -> float:
    return sum(c.get("cost_usd", 0.0) for r in rows for c in r["llm_calls"] if not c["cached"])


# ---------------------------------------------------------------------- stage 1: agent


def run_one(q: dict, verify: bool, spec: str) -> dict:
    llm = Client.from_spec(spec)
    t0 = time.perf_counter()
    with tools.connect() as ro:
        final = build_graph(llm, ro, verify_enabled=verify).invoke(
            initial_state(q["question"], AS_OF)
        )
    wall = time.perf_counter() - t0
    d, ev = final["diagnosis"], final["evidence"]
    cited = {x for c in d["claims"] + d["dropped_claims"] for x in c["evidence_ids"]}
    return {
        "id": q["id"],
        "verify_enabled": verify,
        "diagnosis": d,
        "cited_evidence": {x: ev[x] for x in sorted(cited) if x in ev},
        "evidence_count": len(ev),
        "tool_calls": [
            {k: t[k] for k in ("name", "success", "error", "latency_ms")}
            for t in final["tool_calls"]
        ],
        "llm_calls": final["llm_calls"],
        "loops": final["loops"],
        "verify_log": final["verify_log"],
        "wall_s": round(wall, 2),
    }


def agent_stage(questions, config, split, spec, workers, max_spend) -> None:
    path = runs_path(split, config)
    done = read_jsonl(path)
    todo = [q for q in questions if q["id"] not in done]
    spent = sum(new_spend(read_jsonl(runs_path(split, c)).values()) for c in CONFIGS)
    print(f"{config}: {len(done)} done, {len(todo)} to run; new spend so far ${spent:.4f}")
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        queue = list(todo)
        while queue or pending:
            while queue and len(pending) < workers and spent < max_spend:
                q = queue.pop(0)
                pending[pool.submit(run_one, q, CONFIGS[config], spec)] = q
            if not pending:
                break
            fut = next(as_completed(pending))
            q = pending.pop(fut)
            try:
                row = fut.result()
            except Exception as exc:  # LLM or DB failure: left for the next run
                errors.append(f"{q['id']}: {type(exc).__name__}: {str(exc)[:200]}")
                print(f"  {q['id']} FAILED {errors[-1]}")
                continue
            append_jsonl(path, row)
            spent += new_spend([row])
            claims = len(row["diagnosis"]["claims"])
            print(f"  {q['id']} ok: {claims} claims, {row['wall_s']} s, spend ${spent:.4f}")
    if queue:
        print(f"STOPPED: new spend ${spent:.4f} reached --max-spend ${max_spend}")
    if errors:
        print(f"{len(errors)} run(s) failed and will be retried next time")


# ---------------------------------------------------------------------- stage 2: judge


def judge_stage(questions, config, split, workers) -> None:
    runs, path = read_jsonl(runs_path(split, config)), judge_path(split, config)
    done = read_jsonl(path)
    todo = [q for q in questions if q["id"] in runs and q["id"] not in done]
    llm = JudgeClient.for_role("judge")
    llm.limiter = RateLimiter(JUDGE_RPM, JUDGE_RPD, PROGRESS / "judge_requests.json")
    print(f"judge {config}: {len(done)} done, {len(todo)} to judge ({llm.provider}:{llm.model})")

    def one(q):
        r = runs[q["id"]]
        return judge(llm, q, r["diagnosis"], r["cited_evidence"])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one, q): q for q in todo}
        for fut in as_completed(futs):
            q = futs[fut]
            try:
                verdict, calls = fut.result()
            except LLMError as exc:
                print(f"  judge {q['id']} not judged: {str(exc)[:200]}")
                continue
            append_jsonl(
                path,
                {
                    "id": q["id"],
                    "prompt_version": PROMPT_VERSION,
                    "verdict": verdict.model_dump() if verdict else None,
                    "calls": calls,
                },
            )
            print(f"  judge {q['id']} {'ok' if verdict else 'INVALID twice'}")


# ---------------------------------------------------------------------- stage 3: score


def claim_ok(c: dict, evidence: dict) -> bool:
    ids = c["evidence_ids"]
    return bool(ids) and all(x in evidence for x in ids) and quote_found(c["quote"], ids, evidence)


def score(q: dict, run: dict, verdict: dict | None) -> dict:
    """Per question: metric -> [numerator, denominator] (None when it does not apply)."""
    d, ev = run["diagnosis"], run["cited_evidence"]
    claims = d["claims"]
    calls = run["tool_calls"]
    s = {
        "citation_a": [sum(claim_ok(c, ev) for c in claims), len(claims)] if claims else None,
        "tool_success": [sum(t["success"] for t in calls), len(calls)] if calls else None,
        "verify_loop": [int(run["loops"] > 0), 1],
        "no_diagnosis": [int(d["summary"] == NO_DIAGNOSIS), 1],
        "citation_b": None,
        "faithfulness": None,
        "completeness": None,
        "premise_corrected": None,
        "unrecorded_respected": None,
    }
    if verdict:
        if claims:
            s["citation_b"] = [sum(c["supported"] for c in verdict["claims"]), len(claims)]
        s["faithfulness"] = [verdict["faithfulness"], 1]
        facts = verdict["key_facts"]
        s["completeness"] = [sum(f["present"] for f in facts), len(facts)]
        if q.get("false_premise"):
            s["premise_corrected"] = [int(bool(verdict["premise_corrected"])), 1]
        if q.get("unrecorded"):
            s["unrecorded_respected"] = [int(bool(verdict["unrecorded_respected"])), 1]
    return s


def aggregate(rows: list[dict]) -> dict:
    out = {}
    for m in METRICS:
        have = [r["scores"][m] for r in rows if r["scores"][m] is not None]
        den = sum(x[1] for x in have)
        out[m] = {
            "value": sum(x[0] for x in have) / den if den else None,
            "n": len(have),
            "den": den,
        }
    calls = [c for r in rows for c in r["llm_calls"]]
    n = len(rows)
    out["per_run"] = {
        "n": n,
        "prompt_tokens": sum(c["prompt_tokens"] for c in calls) / n,
        "completion_tokens": sum(c["completion_tokens"] for c in calls) / n,
        "llm_calls": len(calls) / n,
        "tool_calls": sum(len(r["tool_calls"]) for r in rows) / n,
        "cost_usd": sum(c.get("cost_usd", 0.0) for c in calls) / n,
    }
    return out


def slice_rows(rows: list[dict], by_id: dict) -> dict[str, list[dict]]:
    out = {"overall": rows}
    for name, f in SLICES.items():
        for v in sorted({f(by_id[r["id"]]) for r in rows}, key=str):
            out[f"{name}={v}"] = [r for r in rows if f(by_id[r["id"]]) == v]
    return out


def pct(xs: list[float], p: int) -> float | None:
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    return quantiles(xs, n=100, method="inclusive")[p - 1]


def latency(runs: list[dict]) -> dict:
    """From real calls only: a cache hit has no latency."""
    llm = [c["latency_ms"] for r in runs for c in r["llm_calls"] if not c["cached"]]
    tool = [t["latency_ms"] for r in runs for t in r["tool_calls"]]
    wall = [r["wall_s"] for r in runs]
    return {
        "end_to_end_s": {"p50": pct(wall, 50), "p95": pct(wall, 95), "n": len(wall)},
        "llm_call_ms": {"p50": pct(llm, 50), "p95": pct(llm, 95), "n": len(llm)},
        "tool_call_ms": {"p50": pct(tool, 50), "p95": pct(tool, 95), "n": len(tool)},
        "rate_limit_wait_s": sum(c.get("wait_s", 0.0) for r in runs for c in r["llm_calls"]),
    }


def build_rows(questions, config, split) -> tuple[list[dict], list[dict], list[str]]:
    runs, verdicts = read_jsonl(runs_path(split, config)), read_jsonl(judge_path(split, config))
    rows, raw, unjudged = [], [], []
    for q in questions:
        if q["id"] not in runs:
            continue
        r = runs[q["id"]]
        v = (verdicts.get(q["id"]) or {}).get("verdict")
        if v is None:
            unjudged.append(q["id"])
        raw.append(r)
        rows.append(
            {
                "id": q["id"],
                "scores": score(q, r, v),
                "judge": v,
                "claims": len(r["diagnosis"]["claims"]),
                "dropped_claims": len(r["diagnosis"]["dropped_claims"]),
                "loops": r["loops"],
                "llm_calls": [
                    {
                        k: c.get(k, 0.0)
                        for k in ("prompt_tokens", "completion_tokens", "cost_usd", "cached")
                    }
                    for c in r["llm_calls"]
                ],
                "tool_calls": [{"success": t["success"]} for t in r["tool_calls"]],
            }
        )
    return rows, raw, unjudged


def result_content(split, config, rows, questions, unjudged) -> dict:
    s = get_settings()
    by_id = {q["id"]: q for q in questions}
    metrics = {k: aggregate(v) for k, v in slice_rows(rows, by_id).items()}
    content = {
        "split": split,
        "config": {
            "verify_enabled": CONFIGS[config],
            "verify_quotes": s.verify_quotes,
            "max_loops": MAX_LOOPS,
            "as_of": AS_OF,
            "agent_max_tool_calls": s.agent_max_tool_calls,
            "llm_token_budget": s.llm_token_budget,
            "retrieval_mode": s.retrieval_mode,
            "judge_prompt_version": PROMPT_VERSION,
        },
        "models": {"agent": s.agent_llm, "judge": s.judge_llm},
        "questions_total": len(questions),
        "questions_run": len(rows),
        "unjudged": unjudged,
        "metrics": metrics,
        "questions": [
            {k: r[k] for k in ("id", "scores", "judge", "claims", "dropped_claims", "loops")}
            | {
                "tool_calls": len(r["tool_calls"]),
                "llm_calls": len(r["llm_calls"]),
                "prompt_tokens": sum(c["prompt_tokens"] for c in r["llm_calls"]),
                "completion_tokens": sum(c["completion_tokens"] for c in r["llm_calls"]),
                "cost_usd": round(sum(c["cost_usd"] for c in r["llm_calls"]), 8),
            }
            for r in rows
        ],
    }
    return json.loads(json.dumps(content))


def store(split: str, config: str, content: dict) -> None:
    """One eval_runs row (commit, config, models, timestamp) and one eval_results row per
    question, as the retrieval eval does."""
    with psycopg.connect(get_settings().database_url) as conn:
        run_id = conn.execute(
            "INSERT INTO eval_runs (git_commit, eval_type, split, config, models)"
            " VALUES (%s, 'generation', %s, %s, %s) RETURNING id",
            (
                provenance()["commit"],
                split,
                json.dumps(content["config"] | {"name": config}),
                json.dumps(content["models"]),
            ),
        ).fetchone()[0]
        for r in content["questions"]:
            conn.execute(
                "INSERT INTO eval_results (run_id, question_id, metrics) VALUES (%s, %s, %s)",
                (run_id, r["id"], json.dumps(r)),
            )


def write_results(questions, config, split) -> dict | None:
    rows, raw, unjudged = build_rows(questions, config, split)
    if len(rows) < len(questions):
        print(f"{config}: {len(rows)} of {len(questions)} questions run; no result file yet")
        return None
    content = result_content(split, config, rows, questions, unjudged)
    extra = provenance() | {
        "latency": latency(raw),
        "spend_usd": round(new_spend(raw), 6),
        "wall_s_total": round(sum(r["wall_s"] for r in raw), 1),
    }
    path = RESULTS / f"{split}_generation_{config}.json"
    wrote = write_named(path, content, extra)
    if wrote:
        store(split, config, content)
    print(f"{path.relative_to(ROOT)}: {'written' if wrote else 'unchanged'}")
    return content


# ---------------------------------------------------------------------- comparison and report


def ratio(pairs: list) -> float | None:
    den = sum(p[1] for p in pairs)
    return sum(p[0] for p in pairs) / den if den else None


def paired_bootstrap(a: dict, b: dict, ids: list[str], seed: int = SEED) -> tuple:
    """a, b: question id -> [num, den]. Difference of pooled ratios (a - b) over the same
    questions, and its 95% percentile CI from resampling questions (pairs kept together)."""
    rng, diffs = random.Random(seed), []
    for _ in range(RESAMPLES):
        pick = rng.choices(ids, k=len(ids))
        ra, rb = ratio([a[i] for i in pick]), ratio([b[i] for i in pick])
        if ra is not None and rb is not None:
            diffs.append(ra - rb)
    diffs.sort()
    point = ratio([a[i] for i in ids]) - ratio([b[i] for i in ids])
    return point, diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs)) - 1]


def compare(on: dict, off: dict, questions: list[dict]) -> list[dict]:
    """Verify on minus off, per metric and slice, over questions scored in both configs."""
    by_id = {q["id"]: q for q in questions}
    qa = {r["id"]: r["scores"] for r in on["questions"]}
    qb = {r["id"]: r["scores"] for r in off["questions"]}
    out = []
    for m in ("citation_a", "citation_b", "faithfulness", "completeness", "tool_success"):
        both = sorted(i for i in qa if i in qb and qa[i][m] is not None and qb[i][m] is not None)
        groups = {"overall": both}
        if m in ("citation_a", "citation_b"):
            for name, f in SLICES.items():
                for v in sorted({f(by_id[i]) for i in both}, key=str):
                    groups[f"{name}={v}"] = [i for i in both if f(by_id[i]) == v]
        for sl, ids in groups.items():
            if not ids:
                continue
            a = {i: qa[i][m] for i in ids}
            b = {i: qb[i][m] for i in ids}
            d, lo, hi = paired_bootstrap(a, b, ids)
            out.append({"metric": m, "slice": sl, "n": len(ids), "diff": d, "lo": lo, "hi": hi})
    return out


def fmt(x, digits=3) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def report(content: dict) -> str:
    cols = ["citation_a", "citation_b", "faithfulness", "completeness", "tool_success"]
    lines = [
        "| slice | n | " + " | ".join(cols) + " | loop rate | no diagnosis |",
        "|---|---|" + "---|" * (len(cols) + 2),
    ]
    for sl, m in content["metrics"].items():
        cells = [f"{fmt(m[c]['value'])} ({m[c]['den']})" for c in cols]
        lines.append(
            f"| {sl} | {m['per_run']['n']} | {' | '.join(cells)} |"
            f" {fmt(m['verify_loop']['value'])} | {fmt(m['no_diagnosis']['value'])} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------- hand-label sheet


def pick_stratified(ids_by: dict[tuple, list[str]], n: int, seed: int) -> list[str]:
    """n ids across strata in proportion (largest remainder), each stratum sampled with seed."""
    total = sum(map(len, ids_by.values()))
    share = {k: n * len(v) / total for k, v in sorted(ids_by.items())}
    alloc = {k: int(x) for k, x in share.items()}
    for k in sorted(share, key=lambda k: (alloc[k] - share[k], k))[: n - sum(alloc.values())]:
        alloc[k] += 1
    rng = random.Random(seed)
    return [i for k in sorted(ids_by) for i in sorted(rng.sample(sorted(ids_by[k]), alloc[k]))]


def sheet(questions, split: str = "dev", seed: int = SEED) -> str:
    runs = read_jsonl(runs_path(split, "verify_on"))
    by_id = {q["id"]: q for q in questions}
    strata = defaultdict(list)
    for i in runs:
        strata[(by_id[i]["difficulty"], by_id[i]["author"])].append(i)
    picked = pick_stratified(strata, SHEET_N, seed)
    mix = Counter((by_id[i]["difficulty"], by_id[i]["author"]) for i in picked)
    out = [
        "# Judge validation: 30 verify-on dev answers, labelled by Prince",
        "",
        f"Seeded (seed {seed}) and stratified by difficulty and author from"
        f" `.cache/gen_eval/{runs_path(split, 'verify_on').name}`: "
        + ", ".join(f"{d}/{a} {n}" for (d, a), n in sorted(mix.items()))
        + ". Generated by `make judge-sheet`; the judge's labels are not shown.",
        "",
        "Replace each `___` with `yes` or `no`:",
        "- **SUPPORTED**: the cited evidence text states what the claim says (all of it, about"
        " this incident). Judge only from the evidence shown.",
        "- **PRESENT**: the diagnosis (summary, likely cause or a claim) states the key fact; a"
        " vaguer or partial statement is `no`. 'not recorded: X' is present if the diagnosis says"
        " X is unknown.",
        "",
        "Then run `make judge-agreement`.",
        "",
    ]
    for n, i in enumerate(picked, 1):
        q, d = by_id[i], runs[i]["diagnosis"]
        ev = runs[i]["cited_evidence"]
        out += [
            f"## {n}. {i} ({q['difficulty']}, {q['author']}"
            + (", false premise" if q.get("false_premise") else "")
            + ")",
            "",
            f"**Question:** {q['question']}",
            "",
            f"**Summary:** {d['summary']}",
            "",
            f"**Likely cause:** {d['likely_cause']}",
            "",
            "**Claims**",
            "",
        ]
        for c_n, c in enumerate(d["claims"], 1):
            out += [f"C{c_n}. {c['text']}", ""]
            for x in c["evidence_ids"]:
                text = " ".join(ev[x]["text"].split()) if x in ev else "(not gathered)"
                out.append(f"> `{x}` {text}")
            out += ["", f"SUPPORTED {i} C{c_n}: ___", ""]
        if not d["claims"]:
            out += ["(no claims)", ""]
        out += ["**Key facts**", ""]
        for k_n, k in enumerate(q["expected_key_facts"], 1):
            out += [f"K{k_n}. {k}", "", f"PRESENT {i} K{k_n}: ___", ""]
    return "\n".join(out).rstrip() + "\n"


LABEL = re.compile(r"^(SUPPORTED|PRESENT) (\S+) ([CK])(\d+): *(yes|no|___)\s*$", re.M | re.I)


def kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa for two raters' yes/no labels."""
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    pa, pb = sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe == 1 else (po - pe) / (1 - pe)


def agreement(text: str, results: dict) -> dict:
    judged = {r["id"]: r["judge"] for r in results["questions"]}
    pairs = {"claims": [], "key_facts": []}
    blank = missing = 0
    for _kind, qid, ck, num, val in LABEL.findall(text):
        if val == "___":
            blank += 1
            continue
        v = judged.get(qid)
        if v is None:
            missing += 1
            continue
        key, field, idx = (
            ("claims", "supported", "claim")
            if ck.upper() == "C"
            else ("key_facts", "present", "fact")
        )
        jl = {x[idx]: x[field] for x in v[key]}
        if int(num) not in jl:
            missing += 1
            continue
        pairs[key].append((val.lower() == "yes", jl[int(num)]))
    out = {"blank": blank, "not_judged": missing}
    for k, ps in [*pairs.items(), ("all", pairs["claims"] + pairs["key_facts"])]:
        out[k] = {
            "n": len(ps),
            "agreement": sum(a == b for a, b in ps) / len(ps) if ps else None,
            "kappa": kappa(ps),
            "prince_yes": sum(a for a, _ in ps),
            "judge_yes": sum(b for _, b in ps),
        }
    return out


# ---------------------------------------------------------------------- main


def main(argv=None) -> int:
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("action", nargs="?", default="run", choices=("run", "sheet", "agreement"))
    ap.add_argument("--split", choices=("dev", "test"), default="dev")
    ap.add_argument("--final-test", action="store_true")
    ap.add_argument("--config", choices=(*CONFIGS, "both"), default="both")
    ap.add_argument("--concurrency", type=int, default=s.eval_concurrency)
    ap.add_argument("--max-spend", type=float, default=4.5, help="USD of new spend, all configs")
    ap.add_argument("--limit", type=int, help="first N questions only (smoke)")
    ap.add_argument("--skip-judge", action="store_true")
    args = ap.parse_args(argv)
    check_split(args.split, args.final_test)
    questions = [q for q in load(QUESTIONS) if q["split"] == args.split]

    if args.action == "sheet":
        SHEET.write_text(sheet(questions, args.split))
        print(f"wrote {SHEET.relative_to(ROOT)}")
        return 0
    if args.action == "agreement":
        path = RESULTS / f"{args.split}_generation_verify_on.json"
        a = agreement(SHEET.read_text(), json.loads(path.read_text()))
        print(json.dumps(a, indent=1))
        if a["all"]["n"] == 0:
            print("no labels filled in yet")
            return 1
        return 0

    if args.limit:
        questions = questions[: args.limit]
    configs = list(CONFIGS) if args.config == "both" else [args.config]
    results = {}
    for config in configs:
        agent_stage(questions, config, args.split, s.agent_llm, args.concurrency, args.max_spend)
        if not args.skip_judge:
            judge_stage(questions, config, args.split, args.concurrency)
        if args.limit:  # a smoke run never writes the named result file
            print(f"--limit {args.limit}: smoke run, no result file")
            continue
        content = write_results(questions, config, args.split)
        if content:
            results[config] = content
            print(report(content))
    if len(results) == 2:
        print("\nverify on minus off, paired bootstrap 95% CI:")
        for r in compare(results["verify_on"], results["verify_off"], questions):
            print(
                f"  {r['metric']:<14} {r['slice']:<26} n={r['n']:<3} {r['diff']:+.3f}"
                f" [{r['lo']:+.3f}, {r['hi']:+.3f}]"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
