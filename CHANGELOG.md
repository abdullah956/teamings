# Changelog

All notable changes to this project are documented here. Each entry covers
what changed after a commit and why. Newest entries on top.

## [Unreleased]

### Added (1.2 — OpenAI target)
- `targets/base.py` rewritten as a real abstract `Target`: class-level
  `provider` string, `__init__(model_name)`, `name` property
  (`"{provider}/{model_name}"`), and abstract `query(prompt, system=None)`
  with a documented contract for return type, `system`-message handling,
  and which exceptions subclasses are allowed to raise.
- `targets/openai_target.py` — concrete `OpenAITarget` using the official
  `openai` SDK. Defaults: `model="gpt-4o-mini"`, `temperature=0`,
  `max_tokens=1024`. `load_dotenv()` at import time. Retries
  `RateLimitError` and `APIConnectionError` 3 times with exponential
  backoff (2s/4s/8s); auth and bad-request errors re-raise immediately.
  DEBUG-level stderr logging per call (model, prompt/response chars,
  latency_ms, retry_count). In-file comment block explains why
  `temperature=0` is the project's load-bearing reproducibility decision.
- `scripts/smoke_test.py` — standalone end-to-end check: instantiates
  `OpenAITarget`, asks "What is 2+2?", prints the response and
  `target.name`. Exits 1 with an actionable message if `OPENAI_API_KEY`
  is missing or the call raises.
- `scripts/__init__.py` placeholder.

### Changed (1.2)
- `pyproject.toml`: pinned `openai>=1.0` in main `dependencies`; added
  `scripts` to `[tool.hatch.build.targets.wheel].packages`.
- `targets/anthropic_target.py` left untouched (still a stub from 1.1).

## [0.1.0] — Scaffold (1.1)

### Changed
- Flattened the project: scaffold contents moved out of `llm-redteam-suite/`
  into the repo root (`teamings/`); the nested directory was removed.
- Kept the pre-existing root `.env.example` (functionally equivalent to
  the scaffold's, with an explanatory header comment) and discarded the
  scaffold copy.
- Kept the pre-existing root `.gitignore` (a superset of the spec's
  minimal list — already covers `.env`, `results/*` with
  `!results/.gitkeep`, `__pycache__`, `.venv`, `.pytest_cache`) and
  discarded the scaffold copy.

### Added
- Initial project scaffold for the LLM red-team evaluation suite.
- `pyproject.toml` declaring Python 3.11+ and runtime deps (httpx, pyyaml,
  python-dotenv, rich, pandas, openai) plus pytest as a dev extra.
- `.env.example` listing the three provider keys (OpenAI primary; Anthropic
  and HuggingFace planned).
- `.gitignore` covering `.env`, `results/*` (preserving `.gitkeep`),
  `__pycache__`, `.venv`, `.pytest_cache`.
- `README.md` skeleton with TODO placeholders for Overview, Methodology,
  Results, Case Studies, Limitations, How to reproduce.
- `LEARNINGS.md` with the "What I learned building this" header.
- `attacks/prompt_injection/example.yaml` — fully-commented schema example
  using a benign `PWNED` canary, with `id`, `category`, `severity`,
  `prompt`, `success_indicators`, `failure_indicators`, `description`,
  `real_world_basis`.
- `targets/` package: abstract `Target` base + OpenAI and Anthropic
  stubs that raise `NotImplementedError`.
- `judge/rule_based.py` stub raising `NotImplementedError`.
- `runner.py` stub raising `NotImplementedError`.
- `tests/__init__.py` placeholder and `results/.gitkeep`.
