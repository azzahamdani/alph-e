# ADR-0002: Python toolchain — uv + ruff + pytest + mypy --strict

- Status: accepted
- Date: 2026-04-21

## Context

The `agent/` codebase is typed-contract-heavy (every box in `docs/diagrams/state-schema.svg` becomes a Pydantic model) and LLM-driven. It needs:

- Fast, reproducible dependency resolution so specialists and CI reach the same state.
- Strict static typing because the whole architecture leans on typed state (IncidentState, CollectorInput/Output, RemediationPlan, etc.) — untyped drift silently breaks the graph.
- Fast lint+format to keep specialist PRs stylistically uniform without human nitpicking.
- A test framework that handles fixtures well (we'll have JSON incident fixtures in `tests/fixtures/incidents/`).

## Decision

| Tool | Role | Config |
|---|---|---|
| **uv** | Dependency resolution, lockfile (`uv.lock`), venv management, invoke entrypoints | `pyproject.toml` + `uv.lock` |
| **ruff** | Lint + format (replaces black, isort, flake8, pyupgrade, etc.) | `[tool.ruff]` in `pyproject.toml` |
| **pytest** | Test runner | `[tool.pytest.ini_options]` in `pyproject.toml` |
| **mypy --strict** | Static type checking | `[tool.mypy]` in `pyproject.toml` with `strict = true` |

Python version floor: **3.12**. Pydantic v2 plus `langgraph` both work best there; `langgraph-checkpoint-postgres` requires 3.11+.

## Consequences

- **+** `uv sync` is orders of magnitude faster than pip/poetry. Specialists iterate quickly; CI cold-starts are cheap.
- **+** `ruff` + `mypy --strict` means PR review focuses on logic, not style or obvious type bugs.
- **+** Single `pyproject.toml` holds everything — no `mypy.ini`, `setup.cfg`, `pytest.ini` sprawl.
- **−** `uv` is younger than `poetry`. If we hit a resolver bug, fallback is `pip-tools` + `requirements.txt` — not `poetry` (switching mid-stream is more expensive than going back to basics).
- **−** `mypy --strict` demands stubs/type hints for third-party libs. For `langgraph` in particular, we may need to add `# type: ignore[...]` comments until upstream catches up. Reviewer should flag broad `# type: ignore` without a specific code.
