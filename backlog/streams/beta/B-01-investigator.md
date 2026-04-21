---
id: B-01
subject: Investigator node — real LLM-driven hypothesis generation
track: beta
depends_on: [F-01, F-02, F-03, F-04]
advances_wi: [WI-010]
---

## Goal

Replace the skeleton `investigator_node` with an LLM-driven node that:

1. Reads the current `IncidentState` slice (alert + hypotheses + findings).
2. Generates / updates `Hypothesis` entries with scores and evidence refs.
3. Picks `current_focus_hypothesis_id` for the next collector dispatch.
4. Increments `investigation_attempts`.

## Requirements

- Use `agent.llm.Client` (F-01) + `agent.llm.structured.complete_typed` (F-03).
- Use the role prompt loaded via `agent.prompts.load("investigator")` (F-02).
- Tool schema: a Pydantic `InvestigatorOutput` model with `hypotheses: list[Hypothesis]`,
  `current_focus_hypothesis_id: str`, `reasoning: str`. No free-form text leaves
  the node — everything passes through Pydantic.
- Honour `investigation_attempts >= 5` cap: if reached, do not call the LLM —
  return early so routing can escalate.
- Respect the role boundary: never mutate `findings`, `actions_taken`, or any
  field this node does not own.

## Deliverables

- Replace the body of `agent/src/agent/orchestrator/nodes/investigator.py`.
- New: `agent/src/agent/orchestrator/nodes/_models.py` for the per-node
  Pydantic output models (shared between Beta tasks).
- Tests:
  - `agent/tests/unit/test_investigator_node.py` — uses a fake LLM client to
    assert: cap-respect, hypothesis merge logic, focus-id selection, attempts
    counter, no boundary violations.

## Acceptance

- `mypy --strict` clean.
- Unit tests pass without network access (fake `Client`).
- Investigator runs cleanly end-to-end inside the graph for the OOM fixture
  when the LLM fixture returns a confirmed hypothesis after 2 ticks.

## Guardrails

- The Investigator does not call collectors directly. It picks the focus and
  routing hands to the Collectors node.
- Hypothesis ids are stable across ticks: never re-mint an id for the same
  text. Use a content hash of `text` if no id was assigned.

## Done signal

Flip `B-01` in [`../dependencies.md`](../dependencies.md) to `done`.
