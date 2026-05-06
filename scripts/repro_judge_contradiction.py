"""Reproduce the LLM-judge verdict-rationale decoupling on a known case.

The 1.7 run produced two top-confidence (0.9) disagreements between the
regex and LLM judges where the LLM judge's `reason` field describes the
model PASSING the attack but its `verdict` field says "fail". Both rows
are attack=cs-legal-clause-008 (context_stuffing).

This script reloads those exact (attack, response) pairs from the run
CSV and re-judges them N times each, with the on-disk cache bypassed,
so we can see whether the contradiction is:

  (a) a stable, reproducible pattern (same verdict + same self-
      contradicting rationale every time)  — methodology finding
  (b) non-deterministic at temperature=0 (different verdicts across
      runs)  — different methodology finding, but same implication

Usage:
    python scripts/repro_judge_contradiction.py [--n 5]

Writes:
    results/repro_judge_contradiction_<timestamp>.csv
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

# Make project root importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from judge.llm_judge import LLMJudge  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_CSV = REPO_ROOT / "results" / "run_20260506_144709.csv"
ATTACK_PATH = REPO_ROOT / "attacks" / "context_stuffing" / "08_legal_clause.yaml"
TARGETS_OF_INTEREST = (
    "openai/gpt-4o-mini",
    "hf/Qwen/Qwen2.5-7B-Instruct",
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=5, help="repro samples per case")
    args = p.parse_args()

    if not RUN_CSV.exists():
        print(f"ERROR: run CSV missing: {RUN_CSV}", file=sys.stderr)
        return 1

    # Find any cs-legal-clause-008 rows; fall back to whatever attack id
    # the file uses if the slug evolves.
    df = pd.read_csv(RUN_CSV)
    rows = df[
        (df["attack_id"].str.contains("legal-clause", na=False))
        & (df["target"].isin(TARGETS_OF_INTEREST))
    ]
    if rows.empty:
        print("ERROR: no matching rows in run CSV", file=sys.stderr)
        return 1

    attack = yaml.safe_load(ATTACK_PATH.read_text())

    # Bypass cache by writing to a throwaway path inside /tmp. The real
    # results/judge_cache.json must NOT be touched — it's the persisted
    # evidence from the original run.
    tmp_cache = Path("/tmp") / f"repro_judge_cache_{datetime.now():%Y%m%d_%H%M%S}.json"
    judge = LLMJudge(cache_path=tmp_cache)

    out_rows = []
    for _, row in rows.iterrows():
        target = row["target"]
        response = row["response"]
        original_verdict = row.get("llm_verdict", "")
        original_reason = row.get("llm_reason", "")
        original_conf = row.get("llm_confidence", "")
        print(f"\n=== {target} | original llm_verdict={original_verdict!r} "
              f"conf={original_conf} ===")
        print(f"original reason: {original_reason}")
        print(f"replaying {args.n} fresh judgments (cache bypassed)...")
        for i in range(args.n):
            # Force a fresh call by clearing the in-memory cache each iter.
            judge._cache.clear()
            r = judge.judge(attack, response)
            print(
                f"  [{i+1}] verdict={r['verdict']:>4}  conf={r['confidence']:.2f}  "
                f"matched_intent={r['matched_intent']}"
            )
            print(f"       reason: {r['reason']}")
            out_rows.append(
                {
                    "target": target,
                    "iter": i + 1,
                    "verdict": r["verdict"],
                    "confidence": r["confidence"],
                    "matched_intent": r["matched_intent"],
                    "reason": r["reason"],
                    "original_verdict": original_verdict,
                    "original_reason": original_reason,
                }
            )

    out_df = pd.DataFrame(out_rows)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = REPO_ROOT / "results" / f"repro_judge_contradiction_{ts}.csv"
    out_df.to_csv(out_path, index=False)

    # Summary: per-target verdict counts across iterations.
    print("\n=== summary: verdict counts across iterations ===")
    print(out_df.groupby(["target", "verdict"]).size())
    print(f"\nfull repro saved to: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
