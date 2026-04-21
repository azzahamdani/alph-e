---
id: F-01
subject: Anthropic LLM client wrapper with prompt caching
track: alpha
depends_on: []
advances_wi: [WI-010, WI-011, WI-012]
---

## Goal

A single `agent.llm.Client` that every reasoning node imports. All model access
flows through it — no direct `anthropic.Anthropic()` calls elsewhere.

## Requirements

- Uses the official `anthropic` Python SDK.
- Reads `ANTHROPIC_API_KEY` from env; raises a clear error if unset.
- Default model: `claude-sonnet-4-6` (MVP1 single-tier per ADR-0001 / arch doc).
- Supports prompt caching via `cache_control: {"type": "ephemeral"}` on the
  system message and on tool definitions where applicable.
- Exposes an async `complete(system: str, messages: list[dict], tools=None,
  max_tokens=4096) -> Response` method.
- Every call logs model, input/output token counts, cache hit stats, and
  latency (F-04 hooks in here).

## Deliverables

- `agent/src/agent/llm/__init__.py`
- `agent/src/agent/llm/client.py` — the `Client` class.
- `agent/src/agent/llm/settings.py` — `LLMSettings` dataclass (model, timeout, retries).
- `agent/tests/unit/test_llm_client.py` — tests with a mocked Anthropic SDK.

## Acceptance

- `uv run mypy --strict src` clean.
- Unit tests cover: missing API key → typed error; successful call returns
  parsed response; cache-control flag is applied to the system prompt; retry
  on transient `APIStatusError`.
- No real network calls in tests.

## Guardrails

- Do not hard-code the model string anywhere outside `settings.py`.
- Do not introduce LangChain-style abstractions — keep the surface minimal.
- Retries: max 2, exponential backoff, only on 429 / 5xx. Never retry on 4xx.

## Done signal

Flip `F-01` in [`../dependencies.md`](../dependencies.md) to `done` after lint + tests pass.
