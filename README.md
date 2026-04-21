# DevOps Agent Lab

Local environment for building an AI-assisted DevOps investigation agent.

## What's here

- **k3d cluster** — lightweight k3s in Docker, with a local image registry
- **kube-prometheus-stack** — Prometheus, Grafana, Alertmanager, node-exporter, kube-state-metrics
- **Loki + Alloy** — log aggregation for the agent to query
- **leaky-service** — Python demo app that deterministically OOMs (the agent's first case)

## Prerequisites

- Docker
- [k3d](https://k3d.io) ≥ 5.7
- kubectl
- helm ≥ 3.14
- [Task](https://taskfile.dev) ≥ 3.38

On macOS:
```
brew install k3d kubectl helm go-task
```

## Quick start

```
task up
```

This runs: `cluster:up` → `monitoring:install` → `demo:deploy`, then prints the URLs.

Watch the OOM loop kick in:
```
task demo:watch
```

You'll see the pod cycle between `Running`, `OOMKilled`, and `CrashLoopBackOff` every ~60 seconds.

## Investigating manually (what the agent will automate)

```
# Confirm OOMKilled in pod events
task demo:describe

# Look at the memory curve in Grafana
open http://localhost:3000
# → Dashboards → Kubernetes / Compute Resources / Pod
# → namespace=demo, pod=leaky-service-...

# Query Prometheus directly
open 'http://localhost:9090/graph?g0.expr=container_memory_working_set_bytes{namespace="demo"}'

# Check app-level metrics (demo_leaked_bytes climbs monotonically)
open 'http://localhost:9090/graph?g0.expr=demo_leaked_bytes'
```

## Teardown

```
task cluster:down     # keeps the registry, fast to restart
task cluster:nuke     # removes everything including cached images
```

## The agent side

Everything above is the **lab substrate** — the cluster the agent investigates.
The agent itself lives in two sibling trees:

- [`agent/`](agent/) — Python orchestrator (LangGraph + FastAPI + Pydantic schemas)
- [`collectors/`](collectors/) — Go services the orchestrator dispatches against
- [`agent-infra/`](agent-infra/) — in-cluster Postgres + MinIO (agent deps)

Quick start (in addition to `task up`):

```
task agent-infra:install    # Postgres + MinIO (Helm, in-cluster)
task agent:install          # uv sync
task agent:test             # schemas + routing + intake
task collectors:test        # go test ./...
task monitoring:alerts      # apply the PrometheusRule that fires the OOM alert
task agent:serve            # FastAPI on :8000 — Alertmanager's target
task collectors:run         # three services on :8001 / :8002 / :8003
```

For host access to the agent-infra services, run in separate terminals:

```
task agent-infra:postgres   # → localhost:5432
task agent-infra:minio      # → localhost:9000 / :9001
```

Design docs and ADRs:

- Architecture: [`docs/devops-agent-architecture.md`](docs/devops-agent-architecture.md)
- Build fleet: [`docs/devops-agent-build-fleet.md`](docs/devops-agent-build-fleet.md)
- Decisions: [`docs/adr/`](docs/adr/) (0001–0007)
- Build-fleet subagents: [`.claude/agents/`](.claude/agents/)
- Work item backlog: [`backlog/`](backlog/)

## Next steps

- Replace the skeleton nodes in `agent/src/agent/orchestrator/nodes/` with real reasoning (WI-010–012)
- Replace the collector stubs in `collectors/cmd/*` with PromQL/LogQL/client-go dispatch (WI-005–007)
- Add a second failure mode (readiness probe timeout) once the first investigation works end-to-end
