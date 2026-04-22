# Team runbook — alph-e

How to run everything in this repo from a fresh clone. Read in order; later
sections assume the earlier ones already worked.

## TL;DR

```
# clone, then in the repo root:
brew install k3d kubectl helm go-task uv go            # macOS prereqs
export ANTHROPIC_API_KEY=sk-ant-...                    # required for the in-cluster agent
task up                                                # cluster + monitoring + agent-infra + demo + collectors + agent + MCP
task monitoring:alerts                                 # PrometheusRule for OOM detection
task agent:forward                                     # localhost:8000 → in-cluster agent intake
task agent:fire                                        # POST the OOM fixture at the intake
```

If the intake returns `{ "accepted": 1, ... }` and `task agent:logs` shows an
incident seeded + routed through the graph, the full in-cluster stack is
healthy. Real agent reasoning is built per the
[stream plan](../backlog/streams/README.md).

If you want the uvicorn `--reload` dev loop instead of a full in-cluster
roll-out, see [section 8](#8-host-side-dev-loop-uvicorn-reload).

---

## 1. What this repo is

Two layers:

1. **Lab substrate.** k3d cluster + kube-prometheus-stack + Loki + Alloy + a
   `leaky-service` workload that deliberately OOMs every ~60s. This is the
   environment the agent investigates.
2. **Agent and collectors.** Python orchestrator (`agent/`) and Go collectors
   (`collectors/`) that talk to the cluster. Currently a typed skeleton —
   schemas, routing, and HTTP seams are real; reasoning logic is stubbed.

Everything is driven through [`Taskfile.yml`](../Taskfile.yml). `task --list`
shows the full menu.

## 2. Prerequisites

Install once. Versions tested April 2026:

| Tool | Min version | Notes |
|---|---|---|
| Docker Desktop | 4.30+ | colima also works on macOS |
| [k3d](https://k3d.io) | 5.7+ | k3s-in-Docker; bundles its own loadbalancer |
| kubectl | 1.30+ | k3s ships with one but install your own too |
| helm | 3.14+ | kube-prometheus-stack chart |
| [Task](https://taskfile.dev) | 3.38+ | the runbook entrypoint |
| [uv](https://docs.astral.sh/uv) | 0.5+ | Python project + venv manager |
| Go | 1.22+ | for the collectors |
| `mc` (MinIO client) | RELEASE.2024+ | only needed if you debug evidence buckets |

macOS:
```
brew install k3d kubectl helm go-task uv go minio/stable/mc
```

You'll also need an **Anthropic API key** (`ANTHROPIC_API_KEY`) once you start
running the real agents — not needed for the skeleton. Get one from
<https://console.anthropic.com>.

## 3. Substrate — the lab cluster

```
task up                # cluster:up → monitoring:install → agent-infra:install →
                       # demo:deploy → agent:secret → collectors:deploy →
                       # agent:deploy → mcp:install
```

This brings up:

- A 1-server / 2-agent k3d cluster called `devops-agent` with Traefik and
  ServiceLB **disabled** — port exposure goes through k3d's built-in LB.
- kube-prometheus-stack (release `kps`): Prometheus, Grafana, Alertmanager,
  node-exporter, kube-state-metrics.
- Loki single-binary + Alloy DaemonSet for log shipping.
- `agent-infra` namespace with Postgres (pgvector) + MinIO (incidents bucket,
  30-day lifecycle).
- The `leaky-service` workload in the `demo` namespace with a 128Mi limit and
  a 2MB/s leak. **Don't fix it** — it's the canned failure mode the agent
  investigates.
- `agent` namespace containing `prom-collector`, `loki-collector`,
  `kube-collector`, and the `agent` orchestrator. All share the `agent-secrets`
  Secret seeded from your `$ANTHROPIC_API_KEY`.

URLs (printed by `task urls`):

| Service | URL | Creds |
|---|---|---|
| Grafana | <http://localhost:3000> | admin / admin (anonymous Admin also enabled) |
| Prometheus | <http://localhost:9090> | — |
| Alertmanager | <http://localhost:9093> | — |
| Agent intake | <http://localhost:8000/webhook/alertmanager> | — (via k3d LB) |

Watch the OOM cycle:
```
task demo:watch       # Running → OOMKilled → CrashLoopBackOff → repeat
task demo:describe    # see "Events" with OOMKilled reason
task demo:logs        # tail the app
```

Tear down when done:
```
task cluster:down     # keep the local registry (faster restart)
task cluster:nuke     # remove cluster AND registry
```

### 3a. Alert rules — must be applied once

The `PodOOMKilled` and `PodMemoryRisingFast` rules live in
[`monitoring/alert-rules.yaml`](../monitoring/alert-rules.yaml). Apply them:
```
task monitoring:alerts
```
Without this, Alertmanager has nothing to fire and the agent never gets called.

### 3b. Editing the demo workload

If you change [`demo-app/`](../demo-app/) you **must** rebuild and redeploy:
```
task demo:build       # builds and pushes :latest to registry.localhost:5000
task demo:deploy      # rollout picks up :latest because imagePullPolicy: Always
```

## 4. Grafana MCP server (optional, but recommended)

The `mcp/` deployment exposes Grafana as a tool surface for AI assistants.
Bring it up after the cluster is healthy:

```
task grafana          # in a separate terminal — port-forward needed for SA token
task mcp:token        # create the Grafana service-account token Secret
task mcp:install      # apply mcp/manifests.yaml
task mcp:forward      # http://localhost:8000/mcp
```

> ⚠ **Port conflict warning.** The k3d loadbalancer maps host :8000 to the
> in-cluster agent intake. `task mcp:forward` and the host-side
> `task agent:serve` also bind :8000. Stop the MCP forward while the
> in-cluster agent is up, or use a different local port via
> `kubectl -n mcp port-forward svc/grafana-mcp 8001:8000`.

Useful debug:
```
task mcp:logs         # follow MCP pod logs
task mcp:uninstall    # tear it back down
```

## 5. Agent infra — Postgres + MinIO (in-cluster)

The agent needs Postgres (LangGraph checkpointer + evidence metadata) and
MinIO (evidence blobs). Both run in-cluster under the `agent-infra`
namespace via Helm — see [ADR-0008](adr/0008-agent-infra-in-cluster.md):

```
task agent-infra:install   # helm install postgres bitnami/postgresql + minio minio/minio
task agent-infra:status    # pods + services + PVCs
task agent-infra:logs      # tail both
task agent-infra:psql      # psql shell via kubectl exec
task agent-infra:uninstall # helm uninstall (keeps PVCs)
task agent-infra:nuke      # uninstall + delete PVCs (wipes data)
```

Host-access port-forwards (each blocks; separate terminal):

| Task | Forward | Creds |
|---|---|---|
| `task agent-infra:postgres` | localhost:5432 | devops / devops, db=`incidents` |
| `task agent-infra:minio` | localhost:9000 (API) + localhost:9001 (console) | minio / minio-dev-secret |

In-cluster service DNS (what the agent + collectors use):

- `postgres.agent-infra.svc.cluster.local:5432`
- `minio.agent-infra.svc.cluster.local:9000`

The `incidents` bucket is created automatically with a 30-day lifecycle rule
(matches `EvidenceRef.expires_at` in the arch doc).

## 6. Python agent — in-cluster

The agent is packaged as [`agent/Dockerfile`](../agent/Dockerfile) (multi-stage
`uv` build) and deployed from [`agent/manifests.yaml`](../agent/manifests.yaml)
into the `agent` namespace.

```
task agent:secret      # (re-)seed agent-secrets Secret from $ANTHROPIC_API_KEY
task agent:image       # docker build + push → registry.localhost:5000/devops-agent:latest
task agent:deploy      # kubectl apply + rollout status
task agent:logs        # follow orchestrator logs
task agent:forward     # port-forward svc/agent :8000 → localhost:8000
task agent:undeploy    # remove Deployment + Service (keeps Secret)
```

The Secret's defaults assume agent-infra is up in-cluster:

| Key | Default |
|---|---|
| `ANTHROPIC_API_KEY` | **must** be exported before `task agent:secret` |
| `POSTGRES_URL` | `postgresql://devops:devops@postgres.agent-infra.svc.cluster.local:5432/incidents` |
| `EVIDENCE_S3_ENDPOINT` | `http://minio.agent-infra.svc.cluster.local:9000` |
| `EVIDENCE_S3_ACCESS_KEY` / `_SECRET_KEY` / `_BUCKET` | `minio` / `minio-dev-secret` / `incidents` |

Override any of these by exporting them before `task agent:secret`.

What's there today:

- Pydantic v2 schemas under [`agent/src/agent/schemas/`](../agent/src/agent/schemas/).
- LangGraph `StateGraph` with all nodes wired and routing per the arch doc.
- FastAPI Intake at `POST /webhook/alertmanager` parsing v4 payloads.
- All reasoning nodes are skeletons returning `RemediationPlan(type="none")`.
- Real reasoning is built per the [stream plan](../backlog/streams/).

## 7. Go collectors — in-cluster

One image ([`collectors/Dockerfile`](../collectors/Dockerfile)), three
Deployments. Each Deployment picks its binary via `command:` in
[`collectors/manifests.yaml`](../collectors/manifests.yaml).

```
task collectors:image      # docker build + push → registry.localhost:5000/devops-collectors:latest
task collectors:deploy     # roll out prom-collector, loki-collector, kube-collector
task collectors:logs       # tail all three, prefixed by pod
task collectors:undeploy   # remove Deployments + Services + ClusterRole/Binding
```

- `prom-collector` (:8001) → `http://kps-prometheus.monitoring.svc.cluster.local:9090`
- `loki-collector` (:8002) → `http://loki.monitoring.svc.cluster.local:3100`
- `kube-collector` (:8003) → in-cluster API via the `kube-collector`
  ServiceAccount + a read-only `ClusterRole` (pods, events, nodes, namespaces,
  services, deployments, replicasets, statefulsets, daemonsets, metrics.k8s.io).

The shared contract (`internal/contract/`) mirrors the Python Pydantic models
byte-for-byte — change one side, change the other.

## 8. Host-side dev loop (uvicorn `--reload`)

When you want the fast edit-save-reload cycle without rebuilding images, run
the agent + collectors on the host. Agent-infra stays in-cluster; you
port-forward Postgres + MinIO + Loki.

In five terminals:

```
# T1-T3 — port-forwards
task agent-infra:postgres   # localhost:5432
task agent-infra:minio      # localhost:9000 / :9001
task loki                   # localhost:3100

# T4 — Go collectors on :8001 / :8002 / :8003
LOKI_URL=http://localhost:3100 task collectors:run

# T5 — Python orchestrator on :8000
task agent:serve            # uvicorn --reload

# then fire the fixture:
task agent:fire
# expect: { "accepted": 1, "ignored": 0, "incidents": ["inc_..."] }
```

`task dev` bundles the first step and prints the remaining commands.

### Real-alert end-to-end (in-cluster only)

With the in-cluster agent up and the alert rules applied
(`task monitoring:alerts`), the `leaky-service` OOM cycle fires
`PodOOMKilled` after ~5 minutes and Alertmanager POSTs it at
`http://agent.agent.svc.cluster.local:8000/webhook/alertmanager` — all
in-cluster. Verify with `task agent:logs`.

The Alertmanager webhook config lives in
[`monitoring/kube-prometheus-stack.values.yaml`](../monitoring/kube-prometheus-stack.values.yaml);
host-side dev cannot receive live Alertmanager pushes (URL points at the
cluster Service), so use `task agent:fire` instead.

## 9. Running the build streams

Real agent logic is built across three parallel tracks coordinated by a
single gate file. See [`backlog/streams/README.md`](../backlog/streams/README.md)
for the protocol and [`backlog/streams/LAUNCH.md`](../backlog/streams/LAUNCH.md)
for the prompt template.

The short version:

- **Alpha** (`backlog/streams/alpha/`) — shared Python plumbing (LLM client,
  prompt loader, structured output, evidence store, signing, checkpointer).
- **Beta** (`backlog/streams/beta/`) — reasoning nodes (Investigator, Planner,
  Dev, Verifier, Reviewer, Coordinator, Collectors dispatch).
- **Gamma** (`backlog/streams/gamma/`) — Go collectors with real
  PromQL/LogQL/client-go dispatch.

Each task declares its `depends_on` set in its frontmatter. Status lives in
[`backlog/streams/dependencies.md`](../backlog/streams/dependencies.md):

1. Pick the next `todo` row whose blockers are all `done`.
2. Flip it to `in_progress`, claim the `Owner` column, commit
   `chore(streams): claim <ID>`.
3. Implement. Lint + tests must pass.
4. Flip the row to `done`, commit `feat(streams): complete <ID>`.

Never mark a row `done` if tests are red. Never silently expand scope —
add a new row instead.

## 10. ADRs and design docs

| Doc | What it covers |
|---|---|
| [`docs/devops-agent-architecture.md`](devops-agent-architecture.md) | Full agent design — IncidentState, hypothesis loop, ActionIntent safety contract, evidence store, escalation. |
| [`docs/devops-agent-build-fleet.md`](devops-agent-build-fleet.md) | How the build fleet (Tech Lead + specialists) operates. |
| [`docs/adr/`](adr/) | Locked-in decisions: 0001 language split, 0002 Python toolchain, 0003 Go toolchain, 0004 LangGraph, 0005 Alertmanager intake, 0006 evidence store, 0007 build-fleet model, 0008 agent-infra in-cluster. |
| [`docs/diagrams/`](diagrams/) | `.mmd` source + `.svg` output for every architecture diagram. To edit: edit the `.mmd`, then `npx --yes -p @mermaid-js/mermaid-cli mmdc -i docs/diagrams/<name>.mmd -o docs/diagrams/<name>.svg -b transparent`, commit both. |

## 11. Common gotchas

- **`Cannot connect to the Docker daemon`** — start Docker Desktop / colima first.
- **`Error from server (Forbidden): pods is forbidden`** — your kubeconfig
  context is wrong. `kubectl config use-context k3d-devops-agent`.
- **`task agent:secret` fails with `ANTHROPIC_API_KEY must be set`** — export
  it before running `task up` or `task agent:secret`. The Secret is required
  for the agent Deployment to start.
- **`task agent:serve` or `task mcp:forward` says `Address already in use`**
  — the k3d loadbalancer holds :8000 for the in-cluster agent. Stop one of
  them, or rebind MCP with `kubectl -n mcp port-forward svc/grafana-mcp 8001:8000`.
- **`task agent:install` complains about Python 3.12+** — install via
  `uv python install 3.12` (uv will manage its own Python).
- **`task collectors:test` fails on a fresh clone** — run
  `cd collectors && go mod tidy` first to resolve the modules.
- **`kube-collector` Finding shows `kube-collector unavailable`** — the SA
  token or RBAC binding is missing. Check
  `kubectl -n agent get sa,clusterrolebinding | grep kube-collector`.
- **Agent can't reach Postgres/MinIO** — confirm the `agent-secrets` Secret
  has the in-cluster DNS endpoints: `kubectl -n agent get secret agent-secrets -o yaml`.
- **`task demo:build` says `denied: requested access to the resource is
  denied`** — the local registry is at `registry.localhost:5000`. If you
  changed your `/etc/hosts`, restore the entry or run `task cluster:nuke`
  and `task up` to recreate.

## 12. Where to ask questions

- Architecture / scope questions → start in
  [`docs/devops-agent-architecture.md`](devops-agent-architecture.md).
- Build process / who-builds-what → [`docs/devops-agent-build-fleet.md`](devops-agent-build-fleet.md).
- "What's left to do" → [`backlog/streams/dependencies.md`](../backlog/streams/dependencies.md)
  is the live status board.
