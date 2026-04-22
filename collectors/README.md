# collectors — Go HTTP services

Three read-only collectors the Python orchestrator dispatches against. Each
serves a single `POST /collect` endpoint accepting a `CollectorInput` and
returning a `CollectorOutput`. MCP wrappers are deferred; the HTTP contract is
the commitment.

## Contract

The JSON shapes in `internal/contract` mirror the Pydantic models in
`agent/src/agent/schemas/collector.py` byte-for-byte. If a field name drifts on
either side, one language is wrong — fix both before merging.

## Services

| Binary | Port | Data source |
|---|---|---|
| `prom-collector` | 8001 | Prometheus HTTP API (`/api/v1/query`, `/api/v1/query_range`) |
| `loki-collector` | 8002 | Loki HTTP API (`/loki/api/v1/query_range`) |
| `kube-collector` | 8003 | `client-go` — kubeconfig (host) or in-cluster ServiceAccount token (`--in-cluster`) |

Each collector writes evidence blobs to MinIO and returns an `EvidenceRef`
pointing at the stored object — metadata goes into Postgres via the Python
`EvidenceClient` (shared contract, separate write paths; see ADR-0006).

## Commands

Host-side dev:

| Task | Command |
|---|---|
| Lint | `make lint` / `task collectors:lint` |
| Test | `make test` / `task collectors:test` |
| Build binaries into `bin/` | `make build` / `task collectors:build` |
| Run all three locally | `make run` / `task collectors:run` |

In-cluster (the default in `task up`):

| Task | What it does |
|---|---|
| `task collectors:image` | `docker build + push` → `registry.localhost:5000/devops-collectors:latest` (one image, three binaries) |
| `task collectors:deploy` | Applies [`manifests.yaml`](manifests.yaml) — three Deployments/Services in the `agent` namespace; kube-collector gets a dedicated ServiceAccount + read-only ClusterRole/Binding. |
| `task collectors:logs` | Tails all three, prefixed by pod. |
| `task collectors:undeploy` | Removes Deployments + Services + RBAC. |

The kube-collector's Deployment passes `--in-cluster`, so it authenticates
with the pod's ServiceAccount token — no kubeconfig mount. See
[`collectors/internal/kube/client.go`](internal/kube/client.go) for both auth paths.
