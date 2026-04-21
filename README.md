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

## Next steps

- Wire up the collector agent (outside-cluster Python process, kubeconfig + Prometheus API)
- Add LangGraph skeleton for central → collector → QA → dev flow
- Add a second failure mode (readiness probe timeout) once the first investigation works end-to-end
