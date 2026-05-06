import argparse
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from judge.rule_based import judge as rule_judge
from targets.openai_target import OpenAITarget

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = (
    "id",
    "category",
    "severity",
    "prompt",
    "success_indicators",
    "failure_indicators",
    "description",
    "real_world_basis",
)
ALLOWED_SEVERITY = {"low", "med", "high"}

# Attack ids matching these prefixes are documentation/examples and
# are excluded from runs. The numbered files (01_..., 02_..., etc.)
# are the real suite.
EXAMPLE_ID_PREFIXES = ("pi_example", "pi-canary")


def load_attacks(attacks_dir: Path, categories: list[str], console: Console) -> list[dict]:
    """Walk attacks_dir, validate each YAML, compile regexes, return clean list.

    Validation errors print a warning and skip the file — never crash.
    """
    if not attacks_dir.exists():
        console.print(f"[red]attacks dir does not exist:[/red] {attacks_dir}")
        return []

    attacks: list[dict] = []
    for path in sorted(attacks_dir.rglob("*.yaml")):
        if categories != ["all"]:
            try:
                rel = path.relative_to(attacks_dir)
            except ValueError:
                rel = path
            top = rel.parts[0] if len(rel.parts) > 1 else None
            if top not in categories:
                continue

        try:
            with path.open() as f:
                attack = yaml.safe_load(f)
        except yaml.YAMLError as e:
            console.print(f"[yellow]skip {path}: YAML parse error: {e}[/yellow]")
            continue

        if not isinstance(attack, dict):
            console.print(f"[yellow]skip {path}: top-level must be a mapping[/yellow]")
            continue

        missing = [f for f in REQUIRED_FIELDS if f not in attack]
        if missing:
            console.print(f"[yellow]skip {path}: missing fields: {missing}[/yellow]")
            continue

        if isinstance(attack.get("id"), str) and attack["id"].startswith(EXAMPLE_ID_PREFIXES):
            continue

        if attack["severity"] not in ALLOWED_SEVERITY:
            console.print(
                f"[yellow]skip {path}: severity must be one of {sorted(ALLOWED_SEVERITY)}, "
                f"got {attack['severity']!r}[/yellow]"
            )
            continue

        bad = False
        for key in ("success_indicators", "failure_indicators"):
            val = attack[key]
            if not isinstance(val, list) or len(val) == 0:
                console.print(f"[yellow]skip {path}: {key} must be a non-empty list[/yellow]")
                bad = True
                break
            if not all(isinstance(p, str) for p in val):
                console.print(f"[yellow]skip {path}: {key} entries must all be strings[/yellow]")
                bad = True
                break
        if bad:
            continue

        try:
            attack["success_indicators"] = [
                re.compile(p, re.IGNORECASE) for p in attack["success_indicators"]
            ]
            attack["failure_indicators"] = [
                re.compile(p, re.IGNORECASE) for p in attack["failure_indicators"]
            ]
        except re.error as e:
            console.print(f"[yellow]skip {path}: invalid regex: {e}[/yellow]")
            continue

        attack["_source_path"] = str(path)
        attacks.append(attack)

    return attacks


def build_target_registry(target_names: list[str]):
    """Map a CLI target name to a Target instance.

    Anthropic and HF are deferred until Prompt 1.6 — surface a clear error.
    """
    registry = {}
    for name in target_names:
        n = name.strip().lower()
        if n == "openai":
            registry[n] = OpenAITarget()
        elif n in ("anthropic", "hf"):
            raise ValueError(
                f"target {n!r} is not implemented yet — wait for Prompt 1.6. "
                f"Available targets right now: openai"
            )
        else:
            raise ValueError(f"unknown target: {n!r}. Available: openai")
    return registry


def render_summary(rows: list[dict], targets: list[str], console: Console) -> None:
    console.print()
    console.print(
        "[bold]Legend[/bold]: verdict='fail' = ATTACK succeeded (model was exploited). "
        "verdict='pass' = model held its line."
    )

    df = pd.DataFrame(rows)
    if df.empty:
        console.print("[yellow]no rows to summarize[/yellow]")
        return

    table = Table(title="Attack success rate by category × target", show_lines=True)
    table.add_column("category", style="bold")
    sorted_targets = sorted(targets)
    for t in sorted_targets:
        table.add_column(t)

    categories = sorted(df["category"].unique())

    def fmt_cell(fails: int, total: int) -> str:
        if total == 0:
            return "-"
        pct = 100.0 * fails / total
        text = f"{fails}/{total} ({pct:.0f}%)"
        if pct >= 50:
            color = "red"
        elif pct >= 20:
            color = "yellow"
        else:
            color = "green"
        return f"[{color}]{text}[/{color}]"

    for cat in categories:
        row = [cat]
        for t in sorted_targets:
            sub = df[(df["category"] == cat) & (df["target"] == t)]
            fails = int((sub["judge_verdict"] == "fail").sum())
            total = int(len(sub))
            row.append(fmt_cell(fails, total))
        table.add_row(*row)

    total_row = ["TOTAL"]
    for t in sorted_targets:
        sub = df[df["target"] == t]
        fails = int((sub["judge_verdict"] == "fail").sum())
        total = int(len(sub))
        total_row.append(fmt_cell(fails, total))
    table.add_row(*total_row, end_section=True)

    console.print(table)


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM red-team harness runner")
    parser.add_argument("--attacks-dir", default="./attacks")
    parser.add_argument("--targets", default="openai")
    parser.add_argument("--categories", default="all")
    parser.add_argument("--output", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    console = Console()

    target_names = [t.strip() for t in args.targets.split(",") if t.strip()]
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]

    try:
        registry = build_target_registry(target_names)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return 2

    attacks_dir = Path(args.attacks_dir)
    attacks = load_attacks(attacks_dir, categories, console)
    if not attacks:
        console.print("[red]no valid attacks loaded — nothing to do[/red]")
        return 1

    if args.limit is not None:
        attacks = attacks[: args.limit]

    pairs = [(a, name, target) for a in attacks for name, target in registry.items()]
    rows: list[dict] = []

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("running", total=len(pairs))
        for attack, target_name, target in pairs:
            progress.update(task, description=f"{attack['id']} → {target.name}")

            t0 = time.monotonic()
            response = ""
            verdict = "error"
            reason = ""
            matched_pattern = None
            try:
                response = target.query(attack["prompt"])
            except Exception as e:
                verdict = "error"
                reason = f"{type(e).__name__}: {e}"
                response = ""
            else:
                result = rule_judge(attack, response)
                verdict = result["verdict"]
                reason = result["reason"]
                matched_pattern = result["matched_pattern"]
            latency_ms = int((time.monotonic() - t0) * 1000)

            rows.append(
                {
                    "attack_id": attack["id"],
                    "category": attack["category"],
                    "severity": attack["severity"],
                    "target": target.name,
                    "prompt": attack["prompt"],
                    "response": response,
                    "judge_verdict": verdict,
                    "judge_reason": reason,
                    "matched_pattern": matched_pattern,
                    "latency_ms": latency_ms,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            progress.advance(task)

    if args.output:
        out_path = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path("results") / f"run_{ts}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out_path, index=False)

    render_summary(rows, [t.name for t in registry.values()], console)
    console.print(f"\n[bold]CSV written to:[/bold] {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
