# LLM Red-Team Suite

A Python harness that runs adversarial prompts against multiple LLM providers
and records pass/fail results.

## Overview

TODO: Describe the project's purpose, scope, and high-level design. Cover
which providers are supported, what an "attack" means in this codebase, and
the difference between testing refusal robustness and eliciting real harm.

## Methodology

TODO: Document how attacks are authored (YAML schema), how the runner
dispatches them to targets, and how the judge decides pass/fail. Include the
six attack categories (prompt_injection, sycophancy, self_consistency,
context_stuffing, goal_hijacking, logic_bombs) and the regex-based success
/ failure indicator logic.

## Results

TODO: Summary table of pass/fail counts per provider, per category. Link to
the raw run artifacts under `results/`.

## Case Studies

TODO: 2-3 narrative writeups of the most interesting findings — what the
attack was, how the model responded, why the result is notable.

## Limitations

TODO: Be honest about what this suite does NOT measure. Examples to fill in:
single-turn only, English only, no multi-modal, regex judges miss semantic
refusals, small sample sizes, no statistical significance testing.

## How to reproduce

TODO: Steps to clone, install with `uv` (or `pip` fallback), copy
`.env.example` to `.env`, fill in keys, and run the harness. Include exact
commands and expected output location.
