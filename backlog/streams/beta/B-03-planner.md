---
id: B-03
subject: Planner node — RemediationPlan + signed ActionIntent
track: beta
depends_on: [F-01, F-02, F-03, F-05]
advances_wi: [WI-011]
---

## Goal

Replace the skeleton `planner_node` so it produces a real `RemediationPlan`
and — when the plan mutates production — a signed `ActionIntent`.

## Requirements

- LLM-driven via the same F-01/F-02/F-03 stack used by Investigator.
- Output Pydantic model: `PlannerOutput` with `plan: RemediationPlan`,
  optional `intent: ActionIntent`, `reasoning: str`.
- Decision discipline (per `prompts/planner.md`):
  - Confidence < 0.5 → `type=none`. No PR.
  - Mutating types (`rollback`, `scale`, `flag_flip`) **must** include an
    `intent` produced via `agent.security.action_intent.Signer.sign(...)` (F-05).
  - `type=pr` does not require an intent (PR proposes; merge is the human gate).
  - `type=none` and `type=runbook` do not require an intent.
- Append the plan and (if any) the intent into `IncidentState`. Phase
  transitions to `planning`.

## Deliverables

- Replace `agent/src/agent/orchestrator/nodes/planner.py`.
- Add `PlannerOutput` to `nodes/_models.py`.
- Tests in `agent/tests/unit/test_planner_node.py` covering: confidence gate,
  intent presence rules per type, signature validity (use F-05 test keypair).

## Acceptance

- `mypy --strict` clean; tests pass without network.
- Round-trip an end-to-end fixture from Investigator output → Planner →
  Coordinator and confirm `IncidentState.action_intents` contains the signed
  intent for a `rollback` plan.

## Guardrails

- Never sign an `ActionIntent` whose hash you didn't compute. Always call
  `Signer.sign()` rather than setting `signature` by hand.
- `requires_human_approval=True` is the default for everything mutable.

## Done signal

Flip `B-03` in [`../dependencies.md`](../dependencies.md) to `done`.
