"""Interactive CLI for human ground-truth judging.

Walks through a JSONL sample produced by ``sample_for_human_judging.py``
and lets a human assign a verdict to each unjudged row. The judge
verdicts (rule, llm) are HIDDEN until after the human commits a
verdict — peeking at them would bias the comparison the whole exercise
exists to measure.

Behaviors:
    * Skips rows that already have a non-empty ``human_verdict``, so
      re-running the script resumes where you left off.
    * Saves progress to the same JSONL file after every committed
      verdict (durable across crashes; safe to ``q`` and resume).
    * After a verdict is committed for a row, prints ``rule said: X,
      llm said: Y`` so you get feedback as you go (helps you stay
      calibrated, but can't bias the verdict that was already saved).

Usage:
    python -m scripts.judge_cli ground_truth/sample_for_judging.jsonl

Per-row keys:
    p   pass — model held its line
    f   fail — attack succeeded (model was exploited)
    s   skip — unsure / case is malformed (excluded from accuracy)
    q   quit — save and exit
    n   notes — add a one-line note to the current row before deciding
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text


REPO_ROOT = Path(__file__).resolve().parents[1]
ATTACKS_DIR = REPO_ROOT / "attacks"


def _load_records(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _save_records(path: Path, records: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    tmp.replace(path)


def _find_attack_yaml(attack_id: str, category: str) -> dict | None:
    """Best-effort load of the originating attack YAML for the description.

    The ``attack_id`` doesn't include the file name, so we glob the
    category subdir and match by the ``id`` field inside each YAML.
    Returns None if no match (the CLI still works — description just
    isn't shown).
    """
    cat_dir = ATTACKS_DIR / category
    if not cat_dir.exists():
        return None
    for yaml_path in sorted(cat_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(yaml_path.read_text())
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and data.get("id") == attack_id:
            return data
    return None


def _render_case(console: Console, rec: dict, idx: int, total: int) -> None:
    attack = _find_attack_yaml(rec["attack_id"], rec["category"])
    description = (attack or {}).get("description", "<no description in YAML>").strip()

    header = (
        f"[bold]Case {idx + 1} of {total}[/bold]   "
        f"attack=[cyan]{rec['attack_id']}[/cyan]   "
        f"category=[magenta]{rec['category']}[/magenta]   "
        f"target=[yellow]{rec['target']}[/yellow]   "
        f"stratum=[dim]{rec['stratum']}[/dim]"
    )
    console.print()
    console.print(header)
    console.print()
    console.print(Panel(description, title="Attack intent", border_style="dim"))
    console.print(Panel(rec["prompt"], title="Adversarial prompt", border_style="blue"))
    console.print(Panel(rec["response"], title="Model response", border_style="green"))


def _reveal(console: Console, rec: dict, human_verdict: str) -> None:
    rule = rec.get("rule_verdict", "?")
    llm = rec.get("llm_verdict", "?")
    rule_match = "[green]✓[/green]" if rule == human_verdict else "[red]✗[/red]"
    llm_match = "[green]✓[/green]" if llm == human_verdict else "[red]✗[/red]"
    msg = Text.from_markup(
        f"  human: [bold]{human_verdict}[/bold]   "
        f"rule said: [bold]{rule}[/bold] {rule_match}   "
        f"llm said: [bold]{llm}[/bold] {llm_match}"
    )
    console.print(msg)
    if rec.get("llm_reason"):
        console.print(f"  [dim]llm reason: {rec['llm_reason']}[/dim]")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path", help="JSONL file from sample_for_human_judging.py")
    args = p.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"ERROR: sample file does not exist: {path}", file=sys.stderr)
        return 1

    console = Console()
    records = _load_records(path)
    total = len(records)
    todo_idxs = [i for i, r in enumerate(records) if not r.get("human_verdict")]
    n_done = total - len(todo_idxs)
    console.print(
        f"[bold]Loaded {total} cases — {n_done} already judged, {len(todo_idxs)} remaining.[/bold]"
    )
    if not todo_idxs:
        console.print("[green]Nothing to do — all cases judged.[/green]")
        return 0
    console.print(
        "Keys: [bold]p[/bold]=pass, [bold]f[/bold]=fail, [bold]s[/bold]=skip, "
        "[bold]n[/bold]=add note then decide, [bold]q[/bold]=save+quit"
    )

    pending_note = ""
    for i in todo_idxs:
        rec = records[i]
        _render_case(console, rec, i, total)

        while True:
            choice = Prompt.ask(
                "Verdict (p / f / s / n / q)",
                choices=["p", "f", "s", "n", "q"],
                default="p",
                show_choices=False,
            ).strip().lower()
            if choice == "n":
                pending_note = Prompt.ask("Note for this case (one line)").strip()
                continue
            break

        if choice == "q":
            console.print("[yellow]Saving and exiting.[/yellow]")
            _save_records(path, records)
            return 0

        verdict_map = {"p": "pass", "f": "fail", "s": "skip"}
        human_verdict = verdict_map[choice]
        rec["human_verdict"] = human_verdict
        rec["human_notes"] = pending_note
        rec["human_judged_at"] = datetime.now(timezone.utc).isoformat()
        pending_note = ""

        _save_records(path, records)
        _reveal(console, rec, human_verdict)

    console.print()
    console.print("[bold green]All cases judged. Run judge_accuracy_report.py next.[/bold green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
