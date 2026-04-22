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
export ANTHROPIC_API_KEY=sk-ant-...        # required for the in-cluster agent
task up
```

This runs: `cluster:up` → `monitoring:install` → `agent-infra:install` → `demo:deploy` → `agent:secret` → `collectors:deploy` → `agent:deploy` → `mcp:install`, then prints the URLs.

Everything (monitoring, agent-infra, collectors, orchestrator, demo) runs in-cluster. The agent intake is reachable at `http://localhost:8000/webhook/alertmanager` via the k3d loadbalancer.

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

The agent lives in three sibling trees, all dockerized and deployed into the `agent` namespace by `task up`:

- [`agent/`](agent/) — Python orchestrator (LangGraph + FastAPI + Pydantic). [`Dockerfile`](agent/Dockerfile), [`manifests.yaml`](agent/manifests.yaml).
- [`collectors/`](collectors/) — Go collectors the orchestrator dispatches against. One [`Dockerfile`](collectors/Dockerfile), three Deployments via [`manifests.yaml`](collectors/manifests.yaml); kube-collector uses in-cluster ServiceAccount auth.
- [`agent-infra/`](agent-infra/) — Helm-managed Postgres (pgvector) + MinIO, 30-day evidence lifecycle.

### In-cluster operations

```
task agent:secret           # (re-)seed agent-secrets from $ANTHROPIC_API_KEY + agent-infra defaults
task agent:deploy           # build + push + roll out the orchestrator
task collectors:deploy      # build + push + roll out prom/loki/kube collectors
task agent:logs             # tail orchestrator logs
task collectors:logs        # tail all three collectors (prefixed by pod)
task agent:forward          # port-forward the intake to localhost:8000 (dev access)
```

### Host-side dev loop (uvicorn `--reload`, no redeploy)

```
task agent-infra:install    # Postgres + MinIO in-cluster
task agent:install          # uv sync
task agent:test             # schemas + routing + intake
task collectors:test        # go test ./...
task monitoring:alerts      # apply the PrometheusRule that fires the OOM alert
task agent:serve            # FastAPI on host :8000 with --reload
task collectors:run         # three host processes on :8001 / :8002 / :8003

# separate terminals, for host access to agent-infra:
task agent-infra:postgres   # → localhost:5432
task agent-infra:minio      # → localhost:9000 / :9001
```

The Alertmanager config POSTs to the in-cluster agent (`http://agent.agent.svc.cluster.local:8000/webhook/alertmanager`); for host-side alert testing, fire fixtures with `task agent:fire` instead.

Design docs and ADRs:

- Architecture: [`docs/devops-agent-architecture.md`](docs/devops-agent-architecture.md)
- Build fleet: [`docs/devops-agent-build-fleet.md`](docs/devops-agent-build-fleet.md)
- Decisions: [`docs/adr/`](docs/adr/) (0001–0008)
- Build-fleet subagents: [`.claude/agents/`](.claude/agents/)
- Work item backlog: [`backlog/`](backlog/)

## Next steps

- Fire the canned OOM fixture end-to-end: `task agent:forward` in one terminal, `task agent:fire` in another (or wait ~5 min for Alertmanager to POST via `task monitoring:alerts`).
- Add a second failure mode (readiness probe timeout) once the first investigation works end-to-end.
