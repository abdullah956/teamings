"""Tests for the human-ground-truth pipeline.

Three things must work for the rigor step to be defensible:

  1. Stratified sampling produces the right counts per (category x target)
     cell — disagreements before agreements, capped by per-stratum size.
  2. The interactive CLI saves and resumes without losing progress, so a
     long judging session that crashes mid-way isn't lost work. Stdin is
     mocked; no real keyboard input.
  3. The accuracy report computes the right confusion matrices on
     synthetic data with known true labels.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.judge_accuracy_report import _compute_stats, _per_category_accuracy
from scripts.sample_for_human_judging import stratified_sample


# ---------------------------------------------------------------------------
# 1. Stratified sampling
# ---------------------------------------------------------------------------


def _synthetic_run_df(n_per_cell: int = 5) -> pd.DataFrame:
    """Build a synthetic --judge both CSV with known disagreement structure.

    Layout: 2 categories x 2 targets = 4 cells. Within each cell:
      * rows 0..1 are disagreements (rule=pass, llm=fail)
      * rows 2..(n-1) are agreements (rule=pass, llm=pass)
    """
    rows = []
    for cat in ("logic_bombs", "prompt_injection"):
        for tgt in ("openai/gpt-4o-mini", "hf/Qwen/Qwen2.5-7B-Instruct"):
            for i in range(n_per_cell):
                if i < 2:
                    rule, llm = "pass", "fail"
                else:
                    rule, llm = "pass", "pass"
                rows.append(
                    {
                        "attack_id": f"{cat[:2]}-x-{i:03d}",
                        "category": cat,
                        "target": tgt,
                        "prompt": "p",
                        "response": "r",
                        "rule_verdict": rule,
                        "rule_reason": "",
                        "rule_matched_pattern": None,
                        "llm_verdict": llm,
                        "llm_confidence": 0.9,
                        "llm_reason": "",
                        "llm_matched_intent": False,
                    }
                )
    return pd.DataFrame(rows)


def test_stratified_sample_respects_caps_per_cell():
    df = _synthetic_run_df(n_per_cell=5)
    records, dis, agr = stratified_sample(
        df, per_disagree=2, per_agree=1, seed=42
    )
    # 4 cells × 2 disagree + 4 cells × 1 agree = 12 records total.
    assert len(records) == 12
    for cell in dis:
        assert dis[cell] == 2, cell
    for cell in agr:
        assert agr[cell] == 1, cell
    # Each record carries its stratum.
    strata = {r["stratum"] for r in records}
    assert strata == {"disagree", "agree"}


def test_stratified_sample_handles_thin_cells():
    """If a cell has fewer disagreements than the cap, take what's there."""
    df = _synthetic_run_df(n_per_cell=5).copy()
    # Force one cell to have only 1 disagreement (mark one of the
    # first-two-rows-of-the-cell as agree).
    mask = (
        (df["category"] == "logic_bombs")
        & (df["target"] == "openai/gpt-4o-mini")
        & (df["attack_id"] == "lo-x-001")
    )
    df.loc[mask, "llm_verdict"] = "pass"
    records, dis, _ = stratified_sample(
        df, per_disagree=2, per_agree=1, seed=42
    )
    cell = ("logic_bombs", "openai/gpt-4o-mini")
    assert dis[cell] == 1


def test_stratified_sample_is_deterministic_under_seed():
    df = _synthetic_run_df(n_per_cell=5)
    a, _, _ = stratified_sample(df, per_disagree=2, per_agree=1, seed=42)
    b, _, _ = stratified_sample(df, per_disagree=2, per_agree=1, seed=42)
    assert [r["attack_id"] for r in a] == [r["attack_id"] for r in b]


def test_stratified_sample_drops_error_rows():
    """A judge='error' row is infrastructure, not methodology. Must be dropped."""
    df = _synthetic_run_df(n_per_cell=5).copy()
    # Mark a single (attack_id, target) row as error and verify that
    # the (id, target) pair never appears in the sample. attack_id alone
    # isn't unique because the synthetic fixture has the same ids across
    # both targets.
    df.loc[0, "rule_verdict"] = "error"
    bad_id = df.loc[0, "attack_id"]
    bad_target = df.loc[0, "target"]
    records, _, _ = stratified_sample(df, per_disagree=2, per_agree=1, seed=42)
    assert not any(
        r["attack_id"] == bad_id and r["target"] == bad_target for r in records
    )


# ---------------------------------------------------------------------------
# 2. CLI save + resume
# ---------------------------------------------------------------------------


def _seed_jsonl(path: Path, n: int = 3) -> None:
    rows = []
    for i in range(n):
        rows.append(
            {
                "attack_id": f"a-{i}",
                "category": "logic_bombs",
                "target": "openai/gpt-4o-mini",
                "stratum": "disagree",
                "prompt": "p",
                "response": "r",
                "rule_verdict": "pass",
                "rule_reason": "",
                "rule_matched_pattern": None,
                "llm_verdict": "fail",
                "llm_confidence": 0.9,
                "llm_reason": "",
                "llm_matched_intent": False,
                "human_verdict": "",
                "human_notes": "",
                "human_judged_at": "",
            }
        )
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_cli_quits_partway_and_resumes(tmp_path, monkeypatch):
    """Verdict on row 0, then quit; second invocation picks up at row 1."""
    sample = tmp_path / "s.jsonl"
    _seed_jsonl(sample, n=3)

    from scripts import judge_cli

    # Force argv and skip the YAML-description loader (no attacks dir
    # under tmp_path); the CLI handles a None attack gracefully.
    monkeypatch.setattr("sys.argv", ["judge_cli", str(sample)])
    monkeypatch.setattr(judge_cli, "_find_attack_yaml", lambda *_a, **_k: None)

    # First session: verdict 'p' on row 0, then 'q' to quit.
    inputs = iter(["p", "q"])
    monkeypatch.setattr(
        "rich.prompt.Prompt.ask",
        lambda *a, **k: next(inputs),
    )
    rc = judge_cli.main()
    assert rc == 0

    # Mid-state: 1 of 3 judged.
    saved = [json.loads(line) for line in sample.read_text().splitlines() if line.strip()]
    judged = [r for r in saved if r["human_verdict"]]
    assert len(judged) == 1
    assert judged[0]["human_verdict"] == "pass"

    # Second session: verdicts on the remaining 2 rows.
    inputs2 = iter(["f", "p"])
    monkeypatch.setattr(
        "rich.prompt.Prompt.ask",
        lambda *a, **k: next(inputs2),
    )
    rc = judge_cli.main()
    assert rc == 0
    saved = [json.loads(line) for line in sample.read_text().splitlines() if line.strip()]
    assert all(r["human_verdict"] in ("pass", "fail", "skip") for r in saved)
    assert len(saved) == 3


def test_cli_skip_is_recorded_and_stays_excluded(tmp_path, monkeypatch):
    sample = tmp_path / "s.jsonl"
    _seed_jsonl(sample, n=1)

    from scripts import judge_cli

    monkeypatch.setattr("sys.argv", ["judge_cli", str(sample)])
    monkeypatch.setattr(judge_cli, "_find_attack_yaml", lambda *_a, **_k: None)
    inputs = iter(["s"])
    monkeypatch.setattr(
        "rich.prompt.Prompt.ask",
        lambda *a, **k: next(inputs),
    )
    rc = judge_cli.main()
    assert rc == 0

    saved = [json.loads(line) for line in sample.read_text().splitlines() if line.strip()]
    assert saved[0]["human_verdict"] == "skip"


# ---------------------------------------------------------------------------
# 3. Accuracy report computations
# ---------------------------------------------------------------------------


def _synthetic_judged() -> list[dict]:
    """Construct a judged sample with hand-computable accuracy.

    Layout (10 rows):
      * rule: TP=2 TN=4 FP=1 FN=3 → correct=6 accuracy=60%
      * llm:  TP=4 TN=4 FP=1 FN=1 → correct=8 accuracy=80%
    """
    cases = [
        # human=fail rows (5 total): rule misses 2, llm misses 1
        ("logic_bombs", "fail", "pass", "fail"),    # rule FN, llm correct
        ("logic_bombs", "fail", "pass", "fail"),    # rule FN, llm correct
        ("logic_bombs", "fail", "pass", "pass"),    # rule FN, llm FN
        ("logic_bombs", "fail", "fail", "fail"),    # both correct
        ("logic_bombs", "fail", "fail", "fail"),    # both correct
        # human=pass rows (5 total): rule has 1 FP, llm has 1 FP
        ("prompt_injection", "pass", "pass", "pass"),
        ("prompt_injection", "pass", "pass", "pass"),
        ("prompt_injection", "pass", "pass", "pass"),
        ("prompt_injection", "pass", "fail", "pass"),  # rule FP
        ("prompt_injection", "pass", "pass", "fail"),  # llm FP
    ]
    out = []
    for cat, h, r, l in cases:
        out.append(
            {
                "category": cat,
                "human_verdict": h,
                "rule_verdict": r,
                "llm_verdict": l,
            }
        )
    return out


def test_accuracy_and_confusion_matrix_on_synthetic_data():
    records = _synthetic_judged()
    rule = _compute_stats("rule", "rule_verdict", records)
    llm = _compute_stats("llm", "llm_verdict", records)

    # Hand-counted from the synthetic layout above.
    assert rule.n == 10
    assert rule.correct == 6
    assert rule.tp == 2
    assert rule.tn == 4
    assert rule.fp == 1
    assert rule.fn == 3
    assert pytest.approx(rule.accuracy, abs=1e-6) == 0.6
    # FNR = FN / (FN+TP) = 3 / (3+2) = 0.6
    assert pytest.approx(rule.fnr, abs=1e-6) == 0.6
    # FPR = FP / (FP+TN) = 1 / (1+4) = 0.2
    assert pytest.approx(rule.fpr, abs=1e-6) == 0.2

    assert llm.n == 10
    assert llm.correct == 8
    assert llm.tp == 4
    assert llm.tn == 4
    assert llm.fp == 1
    assert llm.fn == 1


def test_accuracy_excludes_skipped_and_error_rows():
    records = _synthetic_judged()
    records.append(
        {"category": "x", "human_verdict": "skip", "rule_verdict": "pass", "llm_verdict": "pass"}
    )
    records.append(
        {"category": "x", "human_verdict": "fail", "rule_verdict": "error", "llm_verdict": "fail"}
    )
    rule = _compute_stats("rule", "rule_verdict", records)
    llm = _compute_stats("llm", "llm_verdict", records)
    # Skipped row is excluded from BOTH; error row excluded from rule
    # only (llm has a valid verdict on it).
    assert rule.n == 10
    assert llm.n == 11


def test_per_category_accuracy_breakdown():
    records = _synthetic_judged()
    pc = _per_category_accuracy("rule_verdict", records)
    # logic_bombs: 5 rows, rule correct on 2 of them (the two "both correct")
    assert pc["logic_bombs"] == (2, 5)
    # prompt_injection: 5 rows, rule correct on 4 (one FP)
    assert pc["prompt_injection"] == (4, 5)
