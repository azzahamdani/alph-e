---
id: B-02
subject: Collectors dispatch node — HTTP client + cache
track: beta
depends_on: [F-04, F-06]
advances_wi: [WI-009]
---

## Goal

Replace the skeleton `collectors_node` with one that builds a `CollectorInput`
from the current focus hypothesis, picks the right Go collector, POSTs to
`/collect`, caches by the arch-doc key, and merges the resulting `Finding`
into `IncidentState.findings`.

## Requirements

- HTTP client built on `httpx.AsyncClient`. Endpoints discovered via env vars
  `COLLECTOR_PROM_URL`, `COLLECTOR_LOKI_URL`, `COLLECTOR_KUBE_URL`.
- Selection rule: a small registry mapping hypothesis labels (e.g.
  `metric.*` → prom, `log.*` → loki, `pod.*` / `event.*` → kube). Default to
  prom when uncertain.
- Cache key: `(incident_id, collector_name, question, time_range, scope_services,
  environment_fingerprint)` per arch doc. 5-minute TTL. Backed by the Postgres
  `collector_cache` table introduced in F-06's migration.
- On non-200 from a collector: surface a `Finding` with `confidence=0.0` and
  `summary` describing the failure, then continue. Do not raise into the graph.
- Update `services_touched` to include the collector's `scope_services`.

## Deliverables

- Replace `agent/src/agent/orchestrator/nodes/collectors.py`.
- New: `agent/src/agent/orchestrator/dispatch/__init__.py`,
  `agent/src/agent/orchestrator/dispatch/http.py`,
  `agent/src/agent/orchestrator/dispatch/cache.py`,
  `agent/src/agent/orchestrator/dispatch/registry.py`.
- Tests in `agent/tests/unit/test_collectors_dispatch.py` using `respx` for
  HTTP mocks.

## Acceptance

- `mypy --strict` clean.
- Unit tests cover: selection rules, cache hit/miss, 500 fallback, time-range
  derivation, services_touched merge.
- Integration test (`-m integration`) hits a locally-running prom-collector
  (skeleton output is fine) and asserts the finding round-trips.

## Guardrails

- Never block the graph for more than the per-collector timeout (default 30s).
- Cache misses persist *after* a successful response, never before.

## Done signal

Flip `B-02` in [`../dependencies.md`](../dependencies.md) to `done`.
