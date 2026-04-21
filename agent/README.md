# DevOps agent (Python)

Orchestrator, reasoning agents, typed schemas, and evidence-store client for the DevOps investigation agent.

Architecture: [../docs/devops-agent-architecture.md](../docs/devops-agent-architecture.md).
Build fleet: [../docs/devops-agent-build-fleet.md](../docs/devops-agent-build-fleet.md).
Decisions: [../docs/adr/](../docs/adr/).

## Quick start

```
# From repo root
task infra:up          # Postgres + MinIO
task agent:install     # uv sync
task agent:lint        # ruff + mypy
task agent:test        # pytest
task agent:serve       # uvicorn on :8000 (Intake webhook)
```

## Layout

```
src/agent/
  schemas/       # Pydantic v2 models — single source of truth for the typed state
  intake/        # Alertmanager webhook → IncidentState seed
  orchestrator/  # LangGraph StateGraph — nodes, routing, checkpointing
  evidence/      # MinIO blob + Postgres metadata client
  prompts/       # Role prompts (system.md is the cached prefix)
  cli.py         # `python -m agent` entry point
tests/
  unit/          # Fast, no external deps
  integration/   # Hits local Postgres + MinIO (task infra:up first)
  fixtures/      # Canned Alertmanager payloads, incident snapshots
```

## Toolchain

See [ADR-0002](../docs/adr/0002-python-toolchain-uv-ruff-pytest-mypy.md) for rationale.

- `uv` for dependency resolution and venv.
- `ruff` for lint + format.
- `pytest` (+ pytest-asyncio) for tests.
- `mypy --strict` for type checking.

Python 3.12+.

## Running against the lab

1. `task up` (cluster + monitoring + demo running)
2. `task infra:up` (Postgres + MinIO running)
3. `task monitoring:alerts` (apply PrometheusRule)
4. `task agent:serve` (FastAPI on :8000)
5. Wait ~60s for the leaky-service OOM cycle to fire a `PodOOMKilled` alert.
6. Alertmanager POSTs to `http://host.k3d.internal:8000/webhook/alertmanager` (see [ADR-0005](../docs/adr/0005-intake-entry-alertmanager-webhook.md)).
7. Agent logs show a seeded IncidentState; current MVP1 stub resolves it with `RemediationPlan(type="none")`.
