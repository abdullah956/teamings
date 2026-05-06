import argparse
import logging
import os
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

from judge.consistency import judge_consistency
from judge.llm_judge import LLMJudge, LLMJudgeOutputError
from judge.rule_based import judge as rule_judge
from targets.hf_inference_target import HFInferenceTarget, _MissingHFTokenError
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
# Categories that require a question_id to be useful for the consistency
# group-judge. Missing question_id on these is a load-time skip.
REQUIRES_QUESTION_ID = {"self_consistency"}
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

        if attack["category"] in REQUIRES_QUESTION_ID and not attack.get("question_id"):
            console.print(
                f"[yellow]skip {path}: category {attack['category']!r} requires "
                f"a non-empty question_id field[/yellow]"
            )
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


# Per-call cost estimates in USD. Used by --cost-estimate. These are
# intentionally rough averages; the real cost depends on prompt and
# response token counts. The point is to give the user a sense of
# magnitude before launching a big run.
COST_PER_CALL = {
    "openai-mini": 0.0001,
    "openai-35": 0.0001,
    "hf-qwen": 0.0,
}

# Per-judge-call cost for the LLM judge (gpt-4o-mini). Same magnitude
# as a target call to gpt-4o-mini; the prompt is longer (full attack +
# response embedded) but max_tokens for the structured-output verdict
# is small.
LLM_JUDGE_COST_PER_CALL = 0.0002


def _factory_openai_mini():
    return OpenAITarget.gpt4o_mini()


def _factory_openai_35():
    return OpenAITarget.gpt35_turbo()


def _factory_hf_qwen():
    return HFInferenceTarget()


# Each entry: alias -> (factory, required_env_var_or_None)
TARGET_FACTORIES = {
    "openai-mini": (_factory_openai_mini, "OPENAI_API_KEY"),
    "openai-35": (_factory_openai_35, "OPENAI_API_KEY"),
    "hf-qwen": (_factory_hf_qwen, "HF_TOKEN"),
    # Backwards-compat alias from prompts 1.1–1.5.
    "openai": (_factory_openai_mini, "OPENAI_API_KEY"),
}


def build_target_registry(target_names: list[str], console: Console):
    """Map a CLI target name to a Target instance.

    If a target's required env var is missing OR the factory itself raises
    a missing-credential error, SKIP that target with a warning instead of
    crashing. That way `--targets openai-mini,hf-qwen` still produces a
    one-column run when HF_TOKEN isn't set.
    """
    registry: dict[str, "OpenAITarget | HFInferenceTarget"] = {}
    for name in target_names:
        n = name.strip().lower()
        if n not in TARGET_FACTORIES:
            raise ValueError(
                f"unknown target: {n!r}. Available: {sorted(TARGET_FACTORIES)}"
            )
        factory, required_env = TARGET_FACTORIES[n]
        if required_env and not os.environ.get(required_env):
            console.print(
                f"[yellow][WARN] {required_env} missing, skipping {n}[/yellow]"
            )
            continue
        try:
            registry[n] = factory()
        except _MissingHFTokenError:
            console.print(f"[yellow][WARN] HF_TOKEN missing, skipping {n}[/yellow]")
            continue
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


def render_disagreement_summary(
    rows: list[dict],
    console: Console,
    out_path: Path,
) -> Path | None:
    """Print a per-category disagreement table and write a disagreements CSV.

    Disagreement = rule_verdict and llm_verdict are both valid
    ('pass'/'fail') but differ. Error rows (target outage / judge
    parsing failure) are excluded; we only want methodology-vs-
    methodology delta, not infrastructure noise.

    Returns the path to the disagreements CSV, or ``None`` if no
    disagreements were found.
    """
    df = pd.DataFrame(rows)
    if df.empty or "rule_verdict" not in df.columns or "llm_verdict" not in df.columns:
        return None

    valid = df[
        df["rule_verdict"].isin(["pass", "fail"])
        & df["llm_verdict"].isin(["pass", "fail"])
    ]
    disagree = valid[valid["rule_verdict"] != valid["llm_verdict"]].copy()

    console.print()
    console.print("[bold]JUDGE DISAGREEMENT SUMMARY[/bold]")
    n_total = len(valid)
    n_dis = len(disagree)
    pct = (100.0 * n_dis / n_total) if n_total else 0.0
    console.print(
        f"Total disagreements: {n_dis} / {n_total} "
        f"({pct:.1f}% of validly-judged rows)"
    )

    if n_dis == 0:
        console.print(
            "[dim]Two judges agreed on every row — no disagreement file written.[/dim]"
        )
        return None

    table = Table(title="Disagreements by category", show_lines=True)
    table.add_column("category", style="bold")
    table.add_column("rule=pass, llm=fail", justify="right")
    table.add_column("rule=fail, llm=pass", justify="right")
    for cat in sorted(disagree["category"].unique()):
        sub = disagree[disagree["category"] == cat]
        rp_lf = int(((sub["rule_verdict"] == "pass") & (sub["llm_verdict"] == "fail")).sum())
        rf_lp = int(((sub["rule_verdict"] == "fail") & (sub["llm_verdict"] == "pass")).sum())
        table.add_row(cat, str(rp_lf), str(rf_lp))
    console.print(table)

    # Top-N highest-confidence disagreements get saved with full prompt
    # and response so a human can audit them. We write all disagreements
    # to disk (not just top-5) — file is small and the case studies in
    # the writeup may pull from anywhere.
    if "llm_confidence" in disagree.columns:
        disagree = disagree.sort_values("llm_confidence", ascending=False)

    dis_path = out_path.with_name(out_path.stem.replace("run_", "disagreements_") + ".csv")
    keep_cols = [
        "attack_id",
        "category",
        "severity",
        "target",
        "prompt",
        "response",
        "rule_verdict",
        "rule_reason",
        "rule_matched_pattern",
        "llm_verdict",
        "llm_confidence",
        "llm_reason",
        "llm_matched_intent",
    ]
    disagree[[c for c in keep_cols if c in disagree.columns]].to_csv(
        dis_path, index=False
    )
    console.print(
        f"\nTop disagreements (sorted by llm_confidence) saved to: [bold]{dis_path}[/bold]"
    )
    return dis_path


def apply_consistency_overrides(rows: list[dict], console: Console) -> None:
    """In-place: override rule_based verdicts for self_consistency rows.

    Groups rows by (target, question_id) and runs ``judge_consistency``
    over each group. The CSV's ``judge_verdict`` / ``judge_reason`` /
    ``matched_pattern`` are replaced with the group-judge result, and
    ``judge_used`` is flipped to "consistency" so the override is
    auditable.

    Rows for other categories are left untouched.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for i, row in enumerate(rows):
        if row.get("category") != "self_consistency":
            continue
        qid = row.get("question_id")
        if not qid:
            # Should be impossible because load_attacks enforces
            # question_id for this category, but guard anyway.
            continue
        groups.setdefault((row["target"], qid), []).append(i)

    for (target, qid), idxs in groups.items():
        # Build minimal attack-shaped dicts for the judge — only the id is read
        attacks = [{"id": rows[i]["attack_id"]} for i in idxs]
        responses = [rows[i].get("response") or "" for i in idxs]
        results = judge_consistency(attacks, responses)
        for idx, result in zip(idxs, results):
            rows[idx]["judge_verdict"] = result["verdict"]
            rows[idx]["judge_reason"] = result["reason"]
            rows[idx]["matched_pattern"] = result["matched_pattern"]
            rows[idx]["judge_used"] = "consistency"
        console.print(
            f"[dim]consistency override: target={target} question_id={qid} "
            f"group_size={len(idxs)}[/dim]"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM red-team harness runner")
    parser.add_argument("--attacks-dir", default="./attacks")
    parser.add_argument("--targets", default="openai")
    parser.add_argument("--categories", default="all")
    parser.add_argument("--output", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--cost-estimate",
        action="store_true",
        help="Print an estimated cost for the planned run and prompt before continuing.",
    )
    parser.add_argument(
        "--judge",
        choices=("rule", "llm", "both"),
        default="rule",
        help=(
            "Which judge(s) to use. 'rule' (default) is the regex judge. "
            "'llm' uses an OpenAI model to read intent + response semantically. "
            "'both' runs both judges, makes the LLM verdict primary, and prints "
            "a disagreement summary at the end. The LLM judge caches verdicts "
            "to results/judge_cache.json so re-runs are free."
        ),
    )
    parser.add_argument(
        "--judge-model",
        default="gpt-4o-mini",
        help="OpenAI model id used for the LLM judge (default: gpt-4o-mini).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    console = Console()

    target_names = [t.strip() for t in args.targets.split(",") if t.strip()]
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]

    try:
        registry = build_target_registry(target_names, console)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return 2

    if not registry:
        console.print("[red]no targets available — nothing to do[/red]")
        return 1

    attacks_dir = Path(args.attacks_dir)
    attacks = load_attacks(attacks_dir, categories, console)
    if not attacks:
        console.print("[red]no valid attacks loaded — nothing to do[/red]")
        return 1

    if args.limit is not None:
        attacks = attacks[: args.limit]

    use_llm = args.judge in ("llm", "both")
    use_rule = args.judge in ("rule", "both")
    llm_judge: LLMJudge | None = None
    if use_llm:
        llm_judge = LLMJudge(judge_model_name=args.judge_model)

    if args.cost_estimate:
        n_attacks = len(attacks)
        total_cost = 0.0
        console.print()
        console.print("[bold]Estimated cost for this run[/bold]")
        for alias in registry.keys():
            per = COST_PER_CALL.get(alias, 0.0)
            sub = per * n_attacks
            total_cost += sub
            note = " (free, rate-limited)" if per == 0.0 else ""
            console.print(
                f"  {alias}: {n_attacks} calls × ${per:.4f}{note} = ${sub:.4f}"
            )
        if use_llm:
            n_judge_calls = n_attacks * len(registry)
            judge_cost = LLM_JUDGE_COST_PER_CALL * n_judge_calls
            total_cost += judge_cost
            console.print(
                f"  llm-judge ({args.judge_model}): {n_judge_calls} calls × "
                f"${LLM_JUDGE_COST_PER_CALL:.4f} = ${judge_cost:.4f}  "
                f"[dim](cached after first run)[/dim]"
            )
        console.print(
            f"  [bold]TOTAL: {n_attacks} attacks × {len(registry)} targets "
            f"= {n_attacks * len(registry)} target calls"
            f"{f' + {n_attacks * len(registry)} judge calls' if use_llm else ''}, "
            f"est. ${total_cost:.4f}[/bold]"
        )
        try:
            answer = input("continue? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "y":
            console.print("[yellow]aborted by user[/yellow]")
            return 0

    pairs = [(a, name, target) for a in attacks for name, target in registry.items()]
    total_pairs = len(pairs)
    rows: list[dict] = []

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("running", total=len(pairs))
        for i, (attack, target_name, target) in enumerate(pairs, start=1):
            progress.update(
                task,
                description=f"{target.name} | {attack['id']} | ({i}/{total_pairs} total)",
            )

            t0 = time.monotonic()
            response = ""
            target_error: str | None = None
            try:
                response = target.query(attack["prompt"])
            except Exception as e:
                target_error = f"{type(e).__name__}: {e}"
                response = ""
            latency_ms = int((time.monotonic() - t0) * 1000)

            # Default rule-based verdict columns. We always populate these
            # in --judge both so the CSV has a stable shape across modes.
            rule_verdict = "error" if target_error else "pass"
            rule_reason = target_error or ""
            rule_pattern: str | None = None
            if not target_error and use_rule:
                rj = rule_judge(attack, response)
                rule_verdict = rj["verdict"]
                rule_reason = rj["reason"]
                rule_pattern = rj["matched_pattern"]

            llm_verdict: str | None = None
            llm_confidence: float | None = None
            llm_reason: str | None = None
            llm_matched_intent: bool | None = None
            if use_llm:
                if target_error:
                    llm_verdict = "error"
                    llm_reason = target_error
                else:
                    try:
                        lj = llm_judge.judge(attack, response)
                        llm_verdict = lj["verdict"]
                        llm_confidence = lj["confidence"]
                        llm_reason = lj["reason"]
                        llm_matched_intent = lj["matched_intent"]
                    except LLMJudgeOutputError as e:
                        llm_verdict = "error"
                        llm_reason = f"LLMJudgeOutputError: {e}"

            # Primary verdict columns. When both judges run, the LLM
            # verdict is primary because it reads intent semantically.
            # When only rule is run, primary == rule. When only LLM,
            # primary == LLM. The CSV always carries `judge_verdict` so
            # downstream tooling doesn't have to branch on mode.
            if args.judge == "rule":
                primary_verdict = rule_verdict
                primary_reason = rule_reason
                primary_pattern = rule_pattern
                judge_used = "rule_based"
            elif args.judge == "llm":
                primary_verdict = llm_verdict or "error"
                primary_reason = llm_reason or ""
                primary_pattern = None
                judge_used = "llm"
            else:  # both
                primary_verdict = llm_verdict or "error"
                primary_reason = llm_reason or ""
                primary_pattern = None
                judge_used = "llm"  # primary; rule columns preserved alongside

            row = {
                "attack_id": attack["id"],
                "category": attack["category"],
                "severity": attack["severity"],
                "question_id": attack.get("question_id"),
                "target": target.name,
                "prompt": attack["prompt"],
                "response": response,
                "judge_verdict": primary_verdict,
                "judge_reason": primary_reason,
                "matched_pattern": primary_pattern,
                "judge_used": judge_used,
                "latency_ms": latency_ms,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if args.judge == "both":
                row.update(
                    {
                        "rule_verdict": rule_verdict,
                        "rule_reason": rule_reason,
                        "rule_matched_pattern": rule_pattern,
                        "llm_verdict": llm_verdict,
                        "llm_confidence": llm_confidence,
                        "llm_reason": llm_reason,
                        "llm_matched_intent": llm_matched_intent,
                    }
                )
            rows.append(row)
            progress.advance(task)

    # Second pass: override rule-based verdicts for self_consistency
    # attacks with the consistency group-judge. We group rows by
    # (target, question_id) so that each model is judged against its
    # own answers — never cross-target.
    #
    # Only applied when the rule-based judge is primary. The LLM judge
    # reads each response on its own and doesn't need (or use) the
    # group-modal-answer machinery; running the override under --judge
    # llm/both would silently overwrite the semantic verdict with a
    # different methodology, which is the opposite of what --judge both
    # is meant to compare.
    if args.judge == "rule":
        apply_consistency_overrides(rows, console)

    if args.output:
        out_path = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path("results") / f"run_{ts}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out_path, index=False)

    render_summary(rows, [t.name for t in registry.values()], console)
    console.print(f"\n[bold]CSV written to:[/bold] {out_path}")

    if args.judge == "both":
        render_disagreement_summary(rows, console, out_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
