"""Compute judge accuracy against human ground truth.

Reads the JSONL produced by ``judge_cli.py`` and reports, for both the
rule-based judge and the LLM judge:

  * Overall accuracy: P(judge_verdict == human_verdict)
  * Confusion matrix (judge x human, 2x2): pass/fail × pass/fail
  * False positive rate (FPR): P(judge=fail | human=pass)
        — judge over-fired, marked a safe model as exploited
  * False negative rate (FNR): P(judge=pass | human=fail)
        — judge missed an exploit
  * Per-category accuracy

Skipped rows (human_verdict='skip') and rows with judge='error' are
excluded from the corresponding judge's stats but still listed at the
top so the methodology is auditable.

Outputs:
  * Rich table to stdout
  * Markdown report at ground_truth/judge_accuracy_report.md
    (paste-ready for the writeup)

Usage:
    python -m scripts.judge_accuracy_report ground_truth/sample_for_judging.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class JudgeStats:
    name: str
    n: int
    correct: int
    tp: int  # judge=fail, human=fail (correctly flagged exploit)
    tn: int  # judge=pass, human=pass (correctly cleared)
    fp: int  # judge=fail, human=pass (over-fired)
    fn: int  # judge=pass, human=fail (missed exploit)

    @property
    def accuracy(self) -> float:
        return (self.correct / self.n) if self.n else 0.0

    @property
    def fpr(self) -> float:
        denom = self.fp + self.tn
        return (self.fp / denom) if denom else 0.0

    @property
    def fnr(self) -> float:
        denom = self.fn + self.tp
        return (self.fn / denom) if denom else 0.0


def _compute_stats(name: str, judge_key: str, records: list[dict]) -> JudgeStats:
    n = correct = tp = tn = fp = fn = 0
    for r in records:
        h = r.get("human_verdict")
        j = r.get(judge_key)
        if h not in ("pass", "fail"):
            continue
        if j not in ("pass", "fail"):
            continue
        n += 1
        if j == h:
            correct += 1
        if j == "fail" and h == "fail":
            tp += 1
        elif j == "pass" and h == "pass":
            tn += 1
        elif j == "fail" and h == "pass":
            fp += 1
        elif j == "pass" and h == "fail":
            fn += 1
    return JudgeStats(name, n, correct, tp, tn, fp, fn)


def _per_category_accuracy(judge_key: str, records: list[dict]) -> dict[str, tuple[int, int]]:
    """Return ``{category: (correct, total)}`` for the given judge."""
    out: dict[str, list[int]] = {}
    for r in records:
        h = r.get("human_verdict")
        j = r.get(judge_key)
        if h not in ("pass", "fail") or j not in ("pass", "fail"):
            continue
        cat = r.get("category", "<unknown>")
        out.setdefault(cat, [0, 0])
        out[cat][1] += 1
        if j == h:
            out[cat][0] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def _render_summary(console: Console, stats: list[JudgeStats], n_total: int, n_skipped: int) -> None:
    console.print()
    console.print(
        f"[bold]Sample size:[/bold] {n_total} judged "
        f"({n_skipped} skipped or unjudged-by-human, excluded from stats)"
    )
    table = Table(title="Judge accuracy vs human ground truth", show_lines=True)
    table.add_column("judge", style="bold")
    table.add_column("n")
    table.add_column("accuracy", justify="right")
    table.add_column("FP rate", justify="right")
    table.add_column("FN rate", justify="right")
    for s in stats:
        table.add_row(
            s.name,
            str(s.n),
            f"{s.accuracy*100:.1f}%",
            f"{s.fpr*100:.1f}%",
            f"{s.fnr*100:.1f}%",
        )
    console.print(table)
    console.print()
    console.print("[dim]FP rate = judge said FAIL when human said PASS (over-fired)[/dim]")
    console.print("[dim]FN rate = judge said PASS when human said FAIL (missed an exploit)[/dim]")


def _render_confusion(console: Console, s: JudgeStats) -> None:
    table = Table(title=f"Confusion matrix — {s.name}", show_lines=True)
    table.add_column("")
    table.add_column("human=pass", justify="right")
    table.add_column("human=fail", justify="right")
    table.add_row("judge=pass", str(s.tn), f"[red]{s.fn}[/red]  (missed)")
    table.add_row("judge=fail", f"[red]{s.fp}[/red]  (over-fired)", str(s.tp))
    console.print(table)


def _render_per_category(console: Console, rule_pc: dict, llm_pc: dict) -> None:
    cats = sorted(set(rule_pc) | set(llm_pc))
    table = Table(title="Per-category accuracy", show_lines=True)
    table.add_column("category", style="bold")
    table.add_column("rule", justify="right")
    table.add_column("llm", justify="right")
    for c in cats:
        rc, rt = rule_pc.get(c, (0, 0))
        lc, lt = llm_pc.get(c, (0, 0))
        rs = f"{rc}/{rt} ({100*rc/rt:.0f}%)" if rt else "-"
        ls = f"{lc}/{lt} ({100*lc/lt:.0f}%)" if lt else "-"
        table.add_row(c, rs, ls)
    console.print(table)


def _markdown_report(
    sample_path: Path,
    n_total: int,
    n_skipped: int,
    rule: JudgeStats,
    llm: JudgeStats,
    rule_pc: dict,
    llm_pc: dict,
) -> str:
    def confusion_md(s: JudgeStats) -> str:
        return (
            f"|              | human=pass | human=fail |\n"
            f"|--------------|-----------:|-----------:|\n"
            f"| judge=pass   | {s.tn}     | {s.fn} (missed) |\n"
            f"| judge=fail   | {s.fp} (over-fired) | {s.tp}     |\n"
        )

    cats = sorted(set(rule_pc) | set(llm_pc))
    cat_rows = []
    for c in cats:
        rc, rt = rule_pc.get(c, (0, 0))
        lc, lt = llm_pc.get(c, (0, 0))
        rs = f"{rc}/{rt} ({100*rc/rt:.0f}%)" if rt else "-"
        ls = f"{lc}/{lt} ({100*lc/lt:.0f}%)" if lt else "-"
        cat_rows.append(f"| {c} | {rs} | {ls} |")

    return (
        f"# Judge accuracy vs human ground truth\n"
        f"\n"
        f"Sample: `{sample_path.name}` — {n_total} human-judged cases "
        f"({n_skipped} skipped, excluded from stats).\n"
        f"\n"
        f"## Headline\n"
        f"\n"
        f"| judge | n | accuracy | FP rate | FN rate |\n"
        f"|-------|---|---------:|--------:|--------:|\n"
        f"| rule  | {rule.n} | {rule.accuracy*100:.1f}% | {rule.fpr*100:.1f}% | {rule.fnr*100:.1f}% |\n"
        f"| llm   | {llm.n}  | {llm.accuracy*100:.1f}%  | {llm.fpr*100:.1f}%  | {llm.fnr*100:.1f}% |\n"
        f"\n"
        f"FP rate = judge said FAIL when human said PASS (over-fired).  \n"
        f"FN rate = judge said PASS when human said FAIL (missed an exploit).\n"
        f"\n"
        f"## Confusion — rule judge\n\n{confusion_md(rule)}\n"
        f"## Confusion — llm judge\n\n{confusion_md(llm)}\n"
        f"## Per-category accuracy\n\n"
        f"| category | rule | llm |\n"
        f"|----------|-----:|----:|\n"
        + "\n".join(cat_rows)
        + "\n"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path", help="JSONL with human_verdict filled in")
    p.add_argument(
        "--report-path",
        default=str(REPO_ROOT / "ground_truth" / "judge_accuracy_report.md"),
        help="Path for the markdown report output",
    )
    args = p.parse_args()

    sample_path = Path(args.path)
    if not sample_path.exists():
        print(f"ERROR: sample file missing: {sample_path}", file=sys.stderr)
        return 1

    records = []
    with sample_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))

    n_total = sum(1 for r in records if r.get("human_verdict") in ("pass", "fail"))
    n_skipped = len(records) - n_total
    if n_total == 0:
        print(
            "ERROR: no rows with human_verdict in {pass, fail} — has anyone judged yet?",
            file=sys.stderr,
        )
        return 1

    rule = _compute_stats("rule", "rule_verdict", records)
    llm = _compute_stats("llm", "llm_verdict", records)
    rule_pc = _per_category_accuracy("rule_verdict", records)
    llm_pc = _per_category_accuracy("llm_verdict", records)

    console = Console()
    _render_summary(console, [rule, llm], n_total, n_skipped)
    console.print()
    _render_confusion(console, rule)
    _render_confusion(console, llm)
    console.print()
    _render_per_category(console, rule_pc, llm_pc)

    md = _markdown_report(sample_path, n_total, n_skipped, rule, llm, rule_pc, llm_pc)
    out_path = Path(args.report_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    console.print(f"\n[bold]Markdown report written to:[/bold] {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
