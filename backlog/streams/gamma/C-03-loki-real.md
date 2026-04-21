---
id: C-03
subject: loki-collector — real LogQL dispatch
track: gamma
depends_on: [C-01]
advances_wi: [WI-006]
---

## Goal

Replace the SKELETON in `cmd/loki-collector/main.go` with real Loki
`/loki/api/v1/query_range` calls bounded by `time_range`.

## Requirements

- Build a LogQL expression from `CollectorInput.question` + `scope_services`.
  MVP1 patterns:
  - `log:contains "<phrase>"` → `{namespace="<ns>"} |= "<phrase>"`
  - `log:rate <pattern>` → `sum(rate({…} |~ "<pattern>" [5m]))`
  - `default` → `{namespace="<ns>"} |~ "(?i)error|fatal|panic|oom"` over the window.
- Persist NDJSON-formatted log lines as the evidence blob. One line per
  match, capped at 5_000 lines (configurable via env `LOKI_MAX_LINES`).
- Confidence: `min(1.0, matches / 100)` — purely informational; the
  Investigator weights it.

## Deliverables

- `collectors/internal/loki/client.go` (stdlib HTTP, typed JSON structs).
- `collectors/internal/loki/dispatch.go`.
- Replace `cmd/loki-collector/main.go::lokiCollector.Collect`.
- Tests with `httptest.Server`.

## Acceptance

- Lint + race tests clean.
- Integration test against the lab Loki returns a non-empty NDJSON blob for
  the demo namespace when the leaky-service is logging OOM events.

## Guardrails

- Cap result size — Loki can return tens of MB on a wide window; the cap is
  a safety net, not just a budget.
- Always set the time range; never rely on Loki's defaults.

## Done signal

Flip `C-03` in [`../dependencies.md`](../dependencies.md) to `done`.
