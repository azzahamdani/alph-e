---
id: F-04
subject: LLM-call observability (structlog + cost)
track: alpha
depends_on: [F-01]
advances_wi: [WI-013]
---

## Goal

Every LLM call produces a structured log line with model, latency, prompt/
completion/cache token counts, and a per-call cost estimate. This is the
baseline the eval corpus (WI-013) will measure regressions against.

## Requirements

- `agent.llm.observability.LLMCallRecorder` — context manager wrapped around
  `Client.complete`. Emits a `structlog` event of type `"llm.call"` with:
  - `model`, `role` (Investigator/Planner/...), `incident_id` if present.
  - `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`.
  - `latency_ms`, `est_cost_usd` (using the per-1M-token rate table in code).
  - `error` field on failure.
- A small in-memory aggregator `RunStats` for tests and the eval harness.

## Deliverables

- `agent/src/agent/llm/observability.py`
- `agent/tests/unit/test_llm_observability.py`

## Acceptance

- `mypy --strict` clean.
- Unit test asserts: a successful call writes one `"llm.call"` event with all
  expected keys; a raised exception still writes one event with `error` set.
- Pricing table is a single dict keyed by model id; trivially overridable.

## Guardrails

- Do not log message contents. Token counts only.
- Cost estimate is informational, not a budget gate. A budget gate is
  out-of-scope for MVP1 — open a follow-up if you want it.

## Done signal

Flip `F-04` in [`../dependencies.md`](../dependencies.md) to `done`.
