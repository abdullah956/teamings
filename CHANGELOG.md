# Changelog

All notable changes to this project are documented here. Each entry covers
what changed after a commit and why. Newest entries on top.

## [Unreleased]

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
