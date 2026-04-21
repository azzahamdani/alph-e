---
id: B-05
subject: Verifier node — dry-run checks return typed VerifierResult
track: beta
depends_on: [F-01, F-02, F-03]
advances_wi: [WI-011]
---

## Goal

Replace the skeleton `verifier_node` with one that runs concrete dry-run
checks on a `FixProposal` or queued `ActionIntent` and returns a
`VerifierResult` with a meaningful `kind`.

## Requirements

- LLM-driven via F-01/F-02/F-03 — the LLM picks *which* checks to run and
  interprets the output. The actual checks are deterministic Python:
  - For `FixProposal`: `git apply --check` against the target repo working
    tree; if the change touches Kubernetes manifests, run
    `kubectl apply --dry-run=server` against the lab cluster and capture the
    output verbatim into `VerifierResult.dry_run_output`.
  - For `ActionIntent`: re-derive the precondition described in
    `expected_effect` and assert it still holds against live state.
- Decision rule (per `prompts/verifier.md`):
  - All checks pass → `VerifierResultKind.passed`.
  - Patch failed but the diagnosis is still defensible → `implementation_error`.
  - Live state contradicts the root-cause observation → `diagnosis_invalidated`.
  - Prefer `diagnosis_invalidated` over `implementation_error` when ambiguous.

## Deliverables

- Replace `agent/src/agent/orchestrator/nodes/verifier.py`.
- New: `agent/src/agent/orchestrator/verifier/checks.py` with one function
  per check kind.
- Tests covering: every routing outcome (`passed` / `implementation_error` /
  `diagnosis_invalidated`), bias-toward-`diagnosis_invalidated` rule.

## Acceptance

- `mypy --strict` clean.
- Unit tests pass without a live cluster (mock the kubectl/git invocations).
- Integration test (`-m integration`) runs `kubectl --dry-run=server` against
  the lab cluster for a known good and a known bad manifest.

## Guardrails

- `dry_run_output` carries raw stdout/stderr — never paraphrased. The router
  needs a verbatim signal to make follow-up decisions.
- Do not mutate state during verification. Even `kubectl apply` must be
  `--dry-run=server` only.

## Done signal

Flip `B-05` in [`../dependencies.md`](../dependencies.md) to `done`.
