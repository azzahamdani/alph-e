# DevOps agent (Python)

Orchestrator, reasoning agents, typed schemas, and evidence-store client for the DevOps investigation agent.

Architecture: [../docs/devops-agent-architecture.md](../docs/devops-agent-architecture.md).
Build fleet: [../docs/devops-agent-build-fleet.md](../docs/devops-agent-build-fleet.md).
Decisions: [../docs/adr/](../docs/adr/).

## Quick start

The agent ships as a Docker image + k8s manifest; `task up` at the repo root
deploys it into the cluster along with the collectors and agent-infra.

Dev loop (host-side, `uvicorn --reload`):

```
# From repo root
task agent-infra:install   # Postgres + MinIO (Helm, in-cluster)
task agent-infra:postgres  # port-forward localhost:5432 (separate terminal)
task agent-infra:minio     # port-forward localhost:9000 / :9001
task agent:install         # uv sync
task agent:lint            # ruff + mypy
task agent:test            # pytest
task agent:serve           # uvicorn on :8000 with --reload
```

In-cluster deploy:

```
export ANTHROPIC_API_KEY=sk-ant-...
task agent:secret          # seed agent-secrets Secret in the `agent` namespace
task agent:deploy          # build + push + roll out
task agent:forward         # port-forward svc/agent → localhost:8000
task agent:logs            # tail orchestrator logs
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
  integration/   # Hits Postgres + MinIO via port-forward (task agent-infra:install first)
  fixtures/      # Canned Alertmanager payloads, incident snapshots
Dockerfile       # multi-stage uv build → distroless Python runtime
manifests.yaml   # Namespace + Deployment + Service in the `agent` namespace
```

## Toolchain

See [ADR-0002](../docs/adr/0002-python-toolchain-uv-ruff-pytest-mypy.md) for rationale.

- `uv` for dependency resolution and venv.
- `ruff` for lint + format.
- `pytest` (+ pytest-asyncio) for tests.
- `mypy --strict` for type checking.

Python 3.12+.

## Running against the lab

In-cluster (the default):

1. `export ANTHROPIC_API_KEY=sk-ant-...`
2. `task up` — cluster + monitoring + agent-infra + demo + collectors + agent (in-cluster).
3. `task monitoring:alerts` (apply PrometheusRule).
4. Wait ~5 min for the leaky-service OOM cycle to fire `PodOOMKilled`.
5. Alertmanager POSTs to `http://agent.agent.svc.cluster.local:8000/webhook/alertmanager` (see [ADR-0005](../docs/adr/0005-intake-entry-alertmanager-webhook.md)).
6. `task agent:logs` — seeded IncidentState routed through the graph.

Host-side dev (uvicorn `--reload`):

- Use `task agent:fire` to POST `tests/fixtures/incidents/oom-leaky-service.json`
  at the in-process app. Alertmanager pushes cannot reach the host from the
  cluster (webhook URL points at the in-cluster Service).
