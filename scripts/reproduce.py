"""`make reproduce`: replay the reported test-split generation runs (shipped, then shipped_off)
from the committed response cache, then fail unless the replay is clean.

Exit 1 when any question's run raised (an LLM or DB error; tool-call failures inside a run are
results, not errors), when a config wrote no result file (e.g. a --max-spend stop), or when either
test result file differs from HEAD afterwards. eval/generation_eval.py is frozen at v0.1-frozen,
so this wraps it instead of editing it: the wrappers count and re-raise, they change nothing.
"""

import subprocess
import sys

from eval import generation_eval as ge

FILES = [
    "eval/results/test_generation_shipped.json",
    "eval/results/test_generation_shipped_off.json",
]
TEST = ["run", "--split", "test", "--final-test", "--max-spend", "0.001"]
RUNS = [
    [*TEST, "--config", "shipped"],
    [*TEST, "--config", "shipped_off", "--skip-judge"],
]


def main() -> int:
    errors, unwritten = [], []
    run_one, write_results = ge.run_one, ge.write_results

    def counted_run(*a, **kw):
        try:
            return run_one(*a, **kw)
        except Exception as exc:
            errors.append(exc)
            raise

    def counted_write(questions, config, split):
        content = write_results(questions, config, split)
        if content is None:
            unwritten.append(config)
        return content

    ge.run_one, ge.write_results = counted_run, counted_write
    try:
        for argv in RUNS:
            ge.main(argv)
    finally:
        ge.run_one, ge.write_results = run_one, write_results

    if errors:
        print(f"reproduce: FAILED, {len(errors)} run(s) raised an error")
        return 1
    if unwritten:
        print(f"reproduce: FAILED, no result file written for {', '.join(unwritten)}")
        return 1
    if subprocess.run(["git", "diff", "--exit-code", "--stat", "--", *FILES]).returncode:
        print("reproduce: FAILED, the test result files differ from HEAD")
        return 1
    print("reproduce: OK, both test result files match HEAD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
