# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo actually is

This repo contains both the **lab substrate** for a DevOps investigation agent and the **agent skeleton** itself. The reasoning logic inside each node is still a stub; the substrate, schemas, routing, and HTTP seams are in place.

- A local k3d cluster with kube-prometheus-stack, Loki, and Alloy.
- A deliberately-broken demo workload (`leaky-service`) that OOMs every ~60s — the first canned incident the agent will investigate.
- Architecture docs the agent was built from ([docs/devops-agent-architecture.md](docs/devops-agent-architecture.md), [docs/devops-agent-build-fleet.md](docs/devops-agent-build-fleet.md)).
- ADRs for the locked-in decisions under [docs/adr/](docs/adr/) (0001–0007).
- Python orchestrator skeleton under [agent/](agent/): Pydantic schemas, LangGraph StateGraph, FastAPI intake, routing, stub nodes.
- Go collectors skeleton under [collectors/](collectors/): HTTP services for Prometheus/Loki/kube with a shared contract mirror.
- Host-side infra under [infra/](infra/): docker-compose for Postgres (pgvector) + MinIO with a 30-day lifecycle rule.
- Build-fleet scaffolding under [.claude/agents/](.claude/agents/) + [backlog/](backlog/).

Before writing reasoning logic, check the MVP1 scope in [docs/devops-agent-architecture.md](docs/devops-agent-architecture.md) under "MVP1: PoC deployment" — Claude Sonnet across all reasoning roles; Haiku is a post-MVP candidate for Intake/Coordinator/collectors.

## Commands

Everything is driven through [Taskfile.yml](Taskfile.yml). `task --list` shows all targets.

**Substrate (cluster + monitoring + demo):**
- `task up` — full bootstrap: `cluster:up` → `monitoring:install` → `demo:deploy` → prints URLs.
- `task cluster:down` — delete cluster, **keep** the local registry (faster restart).
- `task cluster:nuke` — delete cluster and registry.
- `task demo:build` — rebuild and push the `leaky-service` image. Required after any edit to [demo-app/](demo-app/); the Deployment uses `imagePullPolicy: Always` so re-deploying picks up the new `:latest`.
- `task demo:watch` / `task demo:describe` / `task demo:logs` — observe the OOM loop.
- `task monitoring:install` — idempotent (`helm upgrade --install`); safe to re-run after tweaking any values file.
- `task monitoring:alerts` — apply [monitoring/alert-rules.yaml](monitoring/alert-rules.yaml) (PodOOMKilled, PodMemoryRisingFast).

**Agent side:**
- `task infra:up | down | logs | psql` — Postgres + MinIO (docker compose under [infra/](infra/)).
- `task agent:install | lint | test | serve | fire` — uv-managed Python orchestrator.
- `task collectors:lint | test | build | run` — Go collector services.
- `task dev` — bring up infra and remind you how to run agent + collectors.

Agent tests live under [agent/tests/](agent/tests/). Collector tests live alongside the packages under [collectors/](collectors/). Lint configs are in [agent/pyproject.toml](agent/pyproject.toml) (ruff + mypy strict) and [collectors/.golangci.yml](collectors/.golangci.yml).

## Architecture notes worth knowing upfront

**k3d cluster topology.** 1 server + 2 agents (`k3s v1.31.4`) with Traefik and ServiceLB **explicitly disabled** in [cluster/k3d-config.yaml](cluster/k3d-config.yaml). Port exposure goes through k3d's built-in loadbalancer (ports attached to `nodeFilters: [loadbalancer]`): Grafana 3000, Prometheus 9090, Alertmanager 9093, demo app 8080. Local registry at `registry.localhost:5000`.

**Monitoring pipeline.** kube-prometheus-stack (release `kps`) is primary; Loki + Alloy are layered on. Loki is registered as a Grafana datasource via a ConfigMap with the `grafana_datasource: "1"` label — Grafana's sidecar picks it up automatically. The `demo` namespace is labeled `monitoring: enabled` so Prometheus scrapes across namespaces, and each workload ships a `ServiceMonitor` CRD alongside its Deployment (see [demo-app/manifests.yaml](demo-app/manifests.yaml)).

**`leaky-service` is deliberately broken.** 128Mi memory limit + 2MB/sec background leak in [demo-app/app.py](demo-app/app.py) → OOMKilled → CrashLoopBackOff cycle. **Do not "fix" this** — it's the canned failure mode the agent is being built to diagnose. `demo_leaked_bytes` is exposed as a Prometheus gauge so the memory curve is visible in Grafana.

**Docs and diagrams.** Each diagram has both a committed `.mmd` source and a pre-rendered `.svg` under [docs/diagrams/](docs/diagrams/). To edit a diagram, edit `docs/diagrams/<name>.mmd`, then re-render the SVG:

```
npx --yes -p @mermaid-js/mermaid-cli mmdc -i docs/diagrams/<name>.mmd -o docs/diagrams/<name>.svg -b transparent
```

Commit both files together. The original `.mmd` sources were recovered from commit `e56fcfc` (architecture doc originally embedded mermaid blocks inline before the SVG split); keep them aligned going forward.

## Layout

- [cluster/](cluster/) — k3d config.
- [monitoring/](monitoring/) — Helm values for kube-prometheus-stack, Loki, Alloy, and the PrometheusRule at [monitoring/alert-rules.yaml](monitoring/alert-rules.yaml).
- [demo-app/](demo-app/) — leaky-service source, Dockerfile, manifests.
- [docs/](docs/) — agent architecture and build-fleet docs, diagrams (`.mmd` + `.svg`), ADRs.
- [agent/](agent/) — Python orchestrator (uv + LangGraph + FastAPI + Pydantic). Schemas, routing, intake, stub nodes.
- [collectors/](collectors/) — Go collectors (Prometheus/Loki/kube). Shared contract mirror in `internal/contract`.
- [infra/](infra/) — host-side docker-compose for Postgres + MinIO.
- [backlog/](backlog/) — WorkItem YAMLs the build fleet dispatches against.
- [.claude/agents/](.claude/agents/) — build-fleet subagent definitions.
