---
id: F-02
subject: Prompt loader with cached system prefix
track: alpha
depends_on: []
advances_wi: [WI-010, WI-011, WI-012]
---

## Goal

A small loader that reads role prompts from `agent/src/agent/prompts/*.md`,
prepends the shared `system.md` prefix, and returns a structure the LLM
client can pass through with `cache_control` set on the prefix.

## Requirements

- `agent.prompts.load(role: str) -> PromptBundle` — `role` is the bare filename
  without `.md` (e.g. `"investigator"`).
- Returns a `PromptBundle` containing:
  - `system_prefix: str` — contents of `system.md` (cacheable).
  - `role_prompt: str` — contents of the role file.
  - `cache_key: str` — stable hash of `system_prefix` for cache observability.
- Reads files at import time once; never on the hot path.
- Raises `PromptNotFoundError` (typed) if the file is missing.

## Deliverables

- `agent/src/agent/prompts/__init__.py` — exports `load`, `PromptBundle`, errors.
- `agent/src/agent/prompts/loader.py` — implementation.
- `agent/tests/unit/test_prompt_loader.py` — covers all role files plus a
  not-found case.

## Acceptance

- `mypy --strict` clean.
- Test asserts every role md in the repo loads cleanly: `intake.md` is allowed
  to be absent (Intake doesn't run an LLM yet), but `investigator.md`,
  `planner.md`, `dev.md`, `verifier.md`, `reviewer.md`, `coordinator.md` must.

## Guardrails

- No templating engine. Plain string substitution only — and only if a node
  actually needs a placeholder, which it shouldn't for MVP1.
- Do not re-read the disk on every call.

## Done signal

Flip `F-02` in [`../dependencies.md`](../dependencies.md) to `done`.
