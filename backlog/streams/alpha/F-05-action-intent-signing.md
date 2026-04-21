---
id: F-05
subject: ActionIntent signer / verifier
track: alpha
depends_on: []
advances_wi: [WI-011, WI-012]
---

## Goal

`ActionIntent.hash` and `ActionIntent.signature` need a real implementation.
Per arch doc 'Safety contract for operational actions': Planner signs;
Coordinator verifies; keys held by distinct identities.

## Requirements

- `agent.security.action_intent` module with:
  - `compute_intent_hash(*, action_type, target, parameters, expected_effect,
    rollback_hint) -> str` — SHA-256 over a canonical JSON serialisation.
    Stable across runs and across Python versions (sorted keys, no whitespace).
  - `Signer` class — Ed25519 keypair from `PLANNER_SIGNING_KEY` env var (PEM-encoded).
    Method `sign(intent: ActionIntent) -> ActionIntent` returns a copy with
    `hash` and `signature` populated.
  - `Verifier` class — verifies a signed intent against `PLANNER_VERIFY_KEY`
    (public, distinct identity per the arch doc). Returns `True` / raises
    `IntentVerificationError`.
- For local dev / tests, expose `generate_test_keypair()` helper.

## Deliverables

- `agent/src/agent/security/__init__.py`
- `agent/src/agent/security/action_intent.py`
- `agent/tests/unit/test_action_intent.py`

## Acceptance

- `mypy --strict` clean.
- Tests cover: hash stability (same inputs → same hash); signature round-trip
  (signed by Planner key, verified by paired public key); tampering detection
  (mutate `parameters` post-sign → verify raises).
- Uses `cryptography` library Ed25519 primitives.

## Guardrails

- Never log the private key, ever. Tests must use `generate_test_keypair()`.
- The hash must include every field listed above. If you skip one, the safety
  property breaks silently.

## Done signal

Flip `F-05` in [`../dependencies.md`](../dependencies.md) to `done`.
