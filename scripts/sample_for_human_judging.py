"""Build a stratified random sample of (attack, target, response) triples
for human ground-truth judging.

This is the rigor step: until we have human verdicts on a held-out
sample, we cannot say whether the regex judge or the LLM judge is
more accurate. Both have known failure modes (regex over-lenient on
subtle compliance; LLM judge has documented verdict-rationale
decoupling — see LEARNINGS.md). The sampled rows are blind-judged
via ``scripts/judge_cli.py``; the resulting accuracy is reported by
``scripts/judge_accuracy_report.py``.

Stratification:
    * Up to ``--per-disagree`` rows (default 2) per (category x target)
      cell where the two judges disagreed — these are the most
      informative cases.
    * Up to ``--per-agree`` rows (default 1) per cell where the two
      judges agreed — sanity check rows.

Cells with fewer rows than the cap contribute what they have (no
padding, no upsampling). Sampling is deterministic given ``--seed``.

Output: JSONL (one record per line), schema below. The CLI tool
reads and writes back to the same file so progress is durable.

Per-row schema::

    {
        "attack_id": str,
        "category": str,
        "target": str,
        "stratum": "disagree" | "agree",
        "prompt": str,                 # full attack prompt
        "response": str,               # full model response
        "rule_verdict": "pass" | "fail" | "error",
        "rule_reason": str,
        "rule_matched_pattern": str | None,
        "llm_verdict": "pass" | "fail" | "error",
        "llm_confidence": float | None,
        "llm_reason": str,
        "llm_matched_intent": bool | None,
        # Filled by the CLI; left empty by the sampler.
        "human_verdict": "" | "pass" | "fail" | "skip",
        "human_notes": "",
        "human_judged_at": "",
    }
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

import pandas as pd


REQUIRED_COLS = (
    "attack_id",
    "category",
    "target",
    "prompt",
    "response",
    "rule_verdict",
    "llm_verdict",
)


def _coerce_optional(value):
    """Convert pandas NaN to None and numpy scalars to plain Python.

    pandas reads CSVs into numpy dtypes; ``json.dumps`` can't serialize
    ``numpy.bool_`` or ``numpy.int64`` directly, which would crash the
    sampler the first time it touched a boolean column.
    """
    if value is None:
        return None
    if isinstance(value, float):
        # NaN check without importing numpy.
        if value != value:
            return None
        return value
    # numpy scalars expose ``.item()`` to get a plain Python value.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (ValueError, TypeError):
            return value
    return value


def _row_to_record(row: pd.Series, stratum: str) -> dict:
    return {
        "attack_id": row["attack_id"],
        "category": row["category"],
        "target": row["target"],
        "stratum": stratum,
        "prompt": row["prompt"],
        "response": row["response"] if isinstance(row["response"], str) else "",
        "rule_verdict": row["rule_verdict"],
        "rule_reason": _coerce_optional(row.get("rule_reason", "")) or "",
        "rule_matched_pattern": _coerce_optional(row.get("rule_matched_pattern")),
        "llm_verdict": row["llm_verdict"],
        "llm_confidence": _coerce_optional(row.get("llm_confidence")),
        "llm_reason": _coerce_optional(row.get("llm_reason", "")) or "",
        "llm_matched_intent": _coerce_optional(row.get("llm_matched_intent")),
        "human_verdict": "",
        "human_notes": "",
        "human_judged_at": "",
    }


def stratified_sample(
    df: pd.DataFrame,
    per_disagree: int,
    per_agree: int,
    seed: int,
) -> tuple[list[dict], Counter, Counter]:
    """Return (records, per_cell_disagree, per_cell_agree) counts.

    Both counters are keyed by ``(category, target)`` so the caller can
    report which cells contributed what.
    """
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"input CSV missing columns required for stratified sampling: "
            f"{missing}. Did you run with --judge both?"
        )

    # Drop error rows — judging "judge said error" against human verdict
    # isn't meaningful (it's an outage, not a methodology choice).
    valid = df[
        df["rule_verdict"].isin(["pass", "fail"])
        & df["llm_verdict"].isin(["pass", "fail"])
    ].copy()
    valid["_disagree"] = valid["rule_verdict"] != valid["llm_verdict"]

    rng = random.Random(seed)
    records: list[dict] = []
    dis_counts: Counter = Counter()
    agr_counts: Counter = Counter()

    for (cat, tgt), cell in valid.groupby(["category", "target"]):
        dis = cell[cell["_disagree"]]
        agr = cell[~cell["_disagree"]]

        if len(dis) > 0 and per_disagree > 0:
            n = min(per_disagree, len(dis))
            picks = rng.sample(list(dis.index), n)
            for idx in picks:
                records.append(_row_to_record(dis.loc[idx], "disagree"))
                dis_counts[(cat, tgt)] += 1

        if len(agr) > 0 and per_agree > 0:
            n = min(per_agree, len(agr))
            picks = rng.sample(list(agr.index), n)
            for idx in picks:
                records.append(_row_to_record(agr.loc[idx], "agree"))
                agr_counts[(cat, tgt)] += 1

    # Stable shuffle so the CLI presents cases in a randomized order
    # that's reproducible from the seed.
    rng.shuffle(records)
    return records, dis_counts, agr_counts


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, help="path to a run_*.csv from --judge both")
    p.add_argument("--output", required=True, help="path for the JSONL sample (will be overwritten)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--per-disagree", type=int, default=2)
    p.add_argument("--per-agree", type=int, default=1)
    args = p.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)
    if not in_path.exists():
        print(f"ERROR: input CSV does not exist: {in_path}", file=sys.stderr)
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(in_path)
    records, dis_counts, agr_counts = stratified_sample(
        df,
        per_disagree=args.per_disagree,
        per_agree=args.per_agree,
        seed=args.seed,
    )

    with out_path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")

    n_dis = sum(dis_counts.values())
    n_agr = sum(agr_counts.values())
    print(f"Wrote {len(records)} cases to {out_path}", file=sys.stderr)
    print(f"  disagree stratum: {n_dis} cases", file=sys.stderr)
    print(f"  agree stratum:    {n_agr} cases", file=sys.stderr)
    print(file=sys.stderr)
    print("Per-cell breakdown (category x target):", file=sys.stderr)
    cells = sorted(set(dis_counts) | set(agr_counts))
    for cat, tgt in cells:
        d = dis_counts.get((cat, tgt), 0)
        a = agr_counts.get((cat, tgt), 0)
        print(f"  {cat:18s} {tgt:32s}  disagree={d}  agree={a}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
