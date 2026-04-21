---
id: F-07
subject: Postgres checkpointer integration test
track: alpha
depends_on: [F-06]
advances_wi: [WI-009]
---

## Goal

Prove the LangGraph Postgres checkpointer wired in
`agent.orchestrator.checkpoint.postgres_checkpointer` actually persists
`IncidentState` across process restarts.

## Requirements

- New integration test that:
  1. Starts the graph against the `infra:up` Postgres.
  2. Runs the graph through Intake → Investigator → END (using the existing
     skeleton nodes — no LLM dependency).
  3. Reads back the checkpoint via the same saver and asserts the final
     `IncidentState.phase` matches.
- Honour the invariant: checkpoint commit happens **after** any evidence blob
  has been durably written. For MVP1 the skeleton nodes don't write evidence,
  but the test asserts the ordering surface (a check that the evidence client
  is closed before the checkpointer commits, when both are configured).

## Deliverables

- `agent/tests/integration/test_checkpoint.py` (`-m integration`).
- Tiny helper `agent/src/agent/orchestrator/run.py` exposing
  `run_once(state: IncidentState, *, checkpointer) -> IncidentState` so the
  test does not duplicate runner wiring.

## Acceptance

- Test passes against `task infra:up`.
- A second test run reuses the checkpoint and short-circuits on the same
  `incident_id` (the saver returns the prior state).

## Guardrails

- Do not invent a separate checkpoint table — use the saver's own schema.
- Do not run real LLMs in this test.

## Done signal

Flip `F-07` in [`../dependencies.md`](../dependencies.md) to `done`.
