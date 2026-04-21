---
id: C-02
subject: prom-collector — real PromQL dispatch
track: gamma
depends_on: [C-01]
advances_wi: [WI-005]
---

## Goal

Replace the SKELETON `Collect` body in `cmd/prom-collector/main.go` with one
that issues real Prometheus HTTP API queries against the lab cluster's
`prometheus.monitoring.svc.cluster.local:9090` (or the env-configured URL).

## Requirements

- Translate the inbound `CollectorInput` into one or more PromQL queries
  bounded by `time_range`. For MVP1, support these question patterns:
  - `metric:rate <metric> <comparator> <threshold>` → `query_range`
  - `metric:value <metric>` → instant `query`
  - `default` → `kube_pod_container_status_last_terminated_reason{namespace=…}`
- Honour `MaxInternalIterations` — at most that many queries per call.
- Persist the raw JSON response body as the evidence blob via the C-01 writer.
- Build the `Finding` summary from the most-recent / highest-magnitude data
  point. Confidence is `min(1.0, samples_above_threshold / total_samples)`.
- Use a context with the collector's overall request timeout (30s) divided
  evenly across iterations.

## Deliverables

- `collectors/internal/prom/client.go` — typed wrapper over the Prometheus
  HTTP API (no third-party client; stdlib `net/http` + a small JSON struct).
- `collectors/internal/prom/dispatch.go` — input → query mapping.
- Replace the body of `cmd/prom-collector/main.go::promCollector.Collect`.
- Tests using `httptest.Server` to fake Prometheus responses.

## Acceptance

- `golangci-lint run ./...` and `go test ./... -race` clean.
- Integration test (`-tags integration`) hits the lab Prometheus via port-forward
  and returns a non-zero `kube_pod_container_status_last_terminated_reason`
  count for the demo namespace when the leak is firing.

## Guardrails

- Never query without a time range. An unbounded `query` against a real
  Prometheus is a foot-gun.
- Surface upstream Prometheus errors verbatim in the `Finding.summary` so the
  Investigator sees the failure rather than a bland 0-confidence result.

## Done signal

Flip `C-02` in [`../dependencies.md`](../dependencies.md) to `done`.
