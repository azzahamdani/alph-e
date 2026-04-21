# Team runbook — alph-e

How to run everything in this repo from a fresh clone. Read in order; later
sections assume the earlier ones already worked.

## TL;DR

```
# clone, then in the repo root:
brew install k3d kubectl helm go-task uv go            # macOS prereqs
task up                                                # cluster + monitoring + demo
task monitoring:alerts                                 # PrometheusRule for OOM detection
task mcp:install && task mcp:token                     # Grafana MCP server (optional)
task infra:up                                          # Postgres + MinIO for the agent
task agent:install && task agent:test                  # Python orchestrator skeleton
task collectors:test                                   # Go collectors skeleton
task agent:serve                                       # Intake on :8000
```

If `task agent:serve` returns 200 to a POSTed Alertmanager fixture, you have
a working substrate plus agent shell. Real agent reasoning is built per the
[stream plan](../backlog/streams/README.md).

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
task up                # cluster:up → monitoring:install → demo:deploy
```

This brings up:

- A 1-server / 2-agent k3d cluster called `devops-agent` with Traefik and
  ServiceLB **disabled** — port exposure goes through k3d's built-in LB.
- kube-prometheus-stack (release `kps`): Prometheus, Grafana, Alertmanager,
  node-exporter, kube-state-metrics.
- Loki single-binary + Alloy DaemonSet for log shipping.
- The `leaky-service` workload in the `demo` namespace with a 128Mi limit and
  a 2MB/s leak. **Don't fix it** — it's the canned failure mode the agent
  investigates.

URLs (printed by `task urls`):

| Service | URL | Creds |
|---|---|---|
| Grafana | <http://localhost:3000> | admin / admin (anonymous Admin also enabled) |
| Prometheus | <http://localhost:9090> | — |
| Alertmanager | <http://localhost:9093> | — |

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

> ⚠ **Port conflict warning.** `task mcp:forward` and `task agent:serve` both
> bind `:8000`. Pick one at a time, or override the agent port with
> `uv run python -m agent serve --port 8001`. Long-term we'll move the agent
> off 8000 — track in [WI-014](../backlog/WI-014-monitoring-alertmanager-route.yaml).

Useful debug:
```
task mcp:logs         # follow MCP pod logs
task mcp:uninstall    # tear it back down
```

## 5. Agent infra — Postgres + MinIO

The agent needs Postgres (LangGraph checkpointer + evidence metadata) and
MinIO (evidence blobs). Both run host-side via docker compose:

```
task infra:up         # boots both containers; minio-init creates the bucket
task infra:logs       # follow logs
task infra:psql       # psql shell into the local Postgres
task infra:down       # stop everything
```

Default creds (override via env if you care):

| Service | URL | Creds |
|---|---|---|
| Postgres | `postgresql://postgres:devops@localhost:5432/incidents` | postgres / devops |
| MinIO API | <http://localhost:9000> | minio / minio-dev-secret |
| MinIO Console | <http://localhost:9001> | minio / minio-dev-secret |

The `incidents` bucket is created automatically with a 30-day lifecycle rule
(matches `EvidenceRef.expires_at` in the arch doc).

## 6. Python agent

```
cd agent              # everything below assumes you are in agent/
uv sync               # resolve deps into .venv
task agent:lint       # ruff check + ruff format --check + mypy --strict
task agent:test       # pytest unit tests
task agent:serve      # FastAPI Intake on :8000
task agent:fire       # POST tests/fixtures/incidents/oom-leaky-service.json
                      # at the in-process app — sanity-check the webhook end-to-end
```

What's there today:

- Pydantic v2 schemas under [`agent/src/agent/schemas/`](../agent/src/agent/schemas/).
- LangGraph `StateGraph` with all nodes wired and routing per the arch doc.
- FastAPI Intake at `POST /webhook/alertmanager` parsing v4 payloads.
- All reasoning nodes are skeletons returning `RemediationPlan(type="none")`.
- Real reasoning is built per the [stream plan](../backlog/streams/).

## 7. Go collectors

```
cd collectors
task collectors:lint  # golangci-lint run ./...
task collectors:test  # go test ./... -race
task collectors:build # binaries land in collectors/bin/
task collectors:run   # serves prom/loki/kube on :8001 / :8002 / :8003
```

Each binary returns a SKELETON `Finding` with `confidence=0.0` until the
Gamma stream replaces them with real PromQL/LogQL/client-go dispatch.

The shared contract (`internal/contract/`) mirrors the Python Pydantic models
byte-for-byte — change one side, change the other.

## 8. End-to-end smoke test

In four terminals:

```
# T1
task infra:up

# T2
cd collectors && task collectors:run     # 8001/8002/8003

# T3
cd agent && task agent:serve             # 8000

# T4 — fire the fixture
cd agent && task agent:fire
# expect: { "accepted": 1, "ignored": 0, "incidents": ["inc_..."] }
```

Once the alert rules are applied (`task monitoring:alerts`) and Alertmanager
is configured to webhook the agent (already in
[`monitoring/kube-prometheus-stack.values.yaml`](../monitoring/kube-prometheus-stack.values.yaml)),
the `leaky-service` OOM cycle will fire `PodOOMKilled` after ~5 minutes and
Alertmanager will POST it at `host.k3d.internal:8000/webhook/alertmanager`
automatically.

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
| [`docs/adr/`](adr/) | Locked-in decisions: 0001 language split, 0002 Python toolchain, 0003 Go toolchain, 0004 LangGraph, 0005 Alertmanager intake, 0006 evidence store, 0007 build-fleet model. |
| [`docs/diagrams/`](diagrams/) | `.mmd` source + `.svg` output for every architecture diagram. To edit: edit the `.mmd`, then `npx --yes -p @mermaid-js/mermaid-cli mmdc -i docs/diagrams/<name>.mmd -o docs/diagrams/<name>.svg -b transparent`, commit both. |

## 11. Common gotchas

- **`Cannot connect to the Docker daemon`** — start Docker Desktop / colima first.
- **`Error from server (Forbidden): pods is forbidden`** — your kubeconfig
  context is wrong. `kubectl config use-context k3d-devops-agent`.
- **`task agent:serve` says `Address already in use`** — almost always the
  MCP port-forward (also :8000). Stop it or use `--port 8001`.
- **`task agent:install` complains about Python 3.12+** — install via
  `uv python install 3.12` (uv will manage its own Python).
- **`task collectors:test` fails on a fresh clone** — run
  `cd collectors && go mod tidy` first to resolve the modules.
- **Alertmanager webhooks fail with `host.k3d.internal: no such host`** — k3d
  ≥ 5.x exposes the host alias by default. If your version doesn't, add
  `--k3s-arg '--kube-apiserver-arg=feature-gates=…'` is **not** the fix — the
  alias is provided by the k3d image. Upgrade k3d.
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
