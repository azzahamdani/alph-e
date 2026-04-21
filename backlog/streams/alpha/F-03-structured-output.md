---
id: F-03
subject: Structured-output helper (Pydantic + retry)
track: alpha
depends_on: [F-01]
advances_wi: [WI-010, WI-011]
---

## Goal

Reasoning nodes return strongly-typed Pydantic models. Anthropic's tool-use
return path gives us JSON; this helper parses it and retries on validation
failure with a corrective message.

## Requirements

- `agent.llm.structured.complete_typed(client, *, system, messages, tool_schema,
  output_model: type[BaseModel], max_retries: int = 1) -> BaseModel`
- On validation failure, append a corrective `user` message describing the
  validation error and retry. Cap at `max_retries`. After that, raise
  `StructuredOutputError` with the last raw output and the validation error.
- The tool schema is generated from the `output_model` via Pydantic's
  `model_json_schema()` — no hand-written JSON schemas in the call sites.

## Deliverables

- `agent/src/agent/llm/structured.py` — the helper.
- `agent/src/agent/llm/errors.py` — typed exception hierarchy.
- `agent/tests/unit/test_structured_output.py` — covers happy path, validation
  failure followed by successful retry, and exhaustion.

## Acceptance

- `mypy --strict` clean.
- Tests use a fake `Client` that returns canned tool-use responses.
- The helper does not silently coerce types — Pydantic strict mode is on.

## Guardrails

- Do not log the full raw model output at INFO level — it can contain prompt
  echoes. Log at DEBUG only.
- Exhaustion is a hard failure, not a fallback to `RemediationPlan(type=none)`.
  The caller decides what to do.

## Done signal

Flip `F-03` in [`../dependencies.md`](../dependencies.md) to `done`.
