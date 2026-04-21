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
| `kube-collector` | 8003 | `client-go`, read-only kubeconfig |

Each collector writes evidence blobs to MinIO and returns an `EvidenceRef`
pointing at the stored object — metadata goes into Postgres via the Python
`EvidenceClient` (shared contract, separate write paths; see ADR-0006).

## Commands

| Task | Command |
|---|---|
| Lint | `make lint` |
| Test | `make test` |
| Build all three binaries | `make build` |
| Run all three locally | `make run` |

`task collectors:*` at the repo root wraps the same targets.
