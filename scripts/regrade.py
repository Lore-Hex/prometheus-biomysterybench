#!/usr/bin/env python3
"""Re-grade a run's stored answers with the LLM judge (no model re-run).

The heuristic `grade_answer` only handles rubrics phrased "answer is X" and
substring-matches, so it silently auto-fails most full-set tasks. This re-judges
each saved answer against its rubric (the faithful scorer) and prints the
corrected aggregate. Operates on the `<private_out>.partial.jsonl` sidecar (or a
private-out JSON's `results`), so it needs no agent loop — just one judge call
per task.

    PROMETHEUSBENCH_API_KEY=... python scripts/regrade.py \
        --results .eval_results_private/glm52_full.partial.jsonl \
        --dataset-dir /path/to/biomysterybench-full \
        --judge-model anthropic/claude-opus-4.8 [--out results/glm52_full.judged.json]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from prometheus_biomysterybench.biomystery import aggregate, grade_answer_llm  # noqa: E402

_KEY_ENV = ("BIOMYSTERY_API_KEY", "PROMETHEUSBENCH_API_KEY", "TRUSTEDROUTER_API_KEY", "TR_API_KEY_FOR_SELF_HEAL")


def _load_rows(path: Path) -> list[dict]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return json.loads(path.read_text())["results"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-grade stored answers with the LLM judge.")
    ap.add_argument("--results", required=True, help="<private_out>.partial.jsonl or a private-out .json")
    ap.add_argument("--dataset-dir", required=True, help="dir with problems.csv (question + answer_rubric)")
    ap.add_argument("--judge-model", default="anthropic/claude-opus-4.8")
    ap.add_argument("--base-url", default="https://api.trustedrouter.com/v1")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out", default=None, help="write the judged rows + aggregate here")
    args = ap.parse_args()

    api_key = args.api_key or next((os.environ[k] for k in _KEY_ENV if os.environ.get(k)), None)
    if not api_key:
        raise SystemExit(f"set --api-key or one of {', '.join(_KEY_ENV)}")
    meta = {
        r["id"]: (r["question"], r["answer_rubric"])
        for r in csv.DictReader((Path(args.dataset_dir) / "problems.csv").open(newline=""))
    }
    rows = _load_rows(Path(args.results))

    def regrade(row: dict) -> dict:
        ans = row.get("final_answer") or ""
        if not ans.strip():
            row["score"] = 0.0
            return row
        q, rubric = meta[row["problem_id"]]
        try:
            row["score"] = grade_answer_llm(
                q, ans, rubric, base_url=args.base_url, api_key=api_key, model=args.judge_model
            )
        except Exception as exc:  # noqa: BLE001 - keep prior score, note the failure
            print(f"  judge error on {row['problem_id']}: {exc!r}", file=sys.stderr)
        return row

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        judged = list(ex.map(regrade, rows))

    agg = aggregate(judged)
    print(json.dumps(agg, indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({"judge_model": args.judge_model, "aggregate": agg, "results": judged}, indent=2))
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
