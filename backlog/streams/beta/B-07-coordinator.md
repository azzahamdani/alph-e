---
id: B-07
subject: Coordinator node — exec, escalation, lifecycle
track: beta
depends_on: [F-05, F-06, B-03]
advances_wi: [WI-012]
---

## Goal

Replace the skeleton `coordinator_node` with the lifecycle owner: precondition
re-check, idempotent execution of signed `ActionIntent`s, escalation package
generation, and partial-failure compensation.

## Requirements

- Signature verification: every `ActionIntent` is verified via
  `agent.security.action_intent.Verifier` (F-05) before any side effect. Fail
  closed on verification error.
- Precondition re-check before mutation. Three outcomes per arch doc:
  diagnosis-invalidating change → route to Investigator; parameter-only drift
  → route to Planner for a fresh intent; already-resolved → short-circuit to
  `IncidentPhase.resolved` with a `no_op` action record.
- Idempotency key = `ActionIntent.hash`. Persist execution records via the
  evidence store (F-06) so retries cannot fan out duplicate mutations.
- For `type=none` plans or exhausted-attempts paths: produce an
  `EscalationPackage` (no "I failed N times" — structured: hypotheses,
  findings, attempts, working theory, suggested next steps). Move incident to
  `IncidentPhase.escalated`.
- Partial-success compensation: derive the inverse from
  `ActionIntent.rollback_hint`, execute, record both forward + compensating
  actions in `actions_taken`, escalate. No unbounded retries.

## Deliverables

- Replace `agent/src/agent/orchestrator/nodes/coordinator.py`.
- New: `agent/src/agent/orchestrator/coordinator/preflight.py`,
  `agent/src/agent/orchestrator/coordinator/exec.py`,
  `agent/src/agent/orchestrator/coordinator/escalation.py`.
- Tests covering: signature failure, each precondition outcome, idempotent
  retry, compensation path, escalation package construction.

## Acceptance

- `mypy --strict` clean.
- Unit tests pass without a live cluster (mock the kube client). Integration
  test exercises a no-op against the lab cluster.
- The escalation package serialises to JSON cleanly and contains every field
  the arch doc lists.

## Guardrails

- Mutations run under a dedicated service identity, **never** the developer's
  ambient `kubectl` context. For MVP1 dev, this is enforced by reading a
  separate kubeconfig at `KUBECONFIG_AGENT`.
- If `Verifier.verify()` raises, the Coordinator must record the failure and
  escalate — it must not retry or attempt to re-sign.

## Done signal

Flip `B-07` in [`../dependencies.md`](../dependencies.md) to `done`.
