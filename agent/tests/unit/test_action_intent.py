"""Unit tests for agent.security.action_intent.

Coverage:
  - hash stability: same inputs always produce the same hash.
  - signature round-trip: Signer signs, Verifier accepts.
  - tampering detection: mutating parameters post-sign causes verify to raise.
  - hash field coverage: changing each hashed field changes the hash.
  - missing env var handling for Signer and Verifier.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest  # noqa: E402

from agent.schemas.incident import ActionIntent, ActionType
from agent.security.action_intent import (
    IntentVerificationError,
    Signer,
    Verifier,
    compute_intent_hash,
    generate_test_keypair,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(offset_minutes: int = 0) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=offset_minutes)


def _make_intent(**overrides: object) -> ActionIntent:
    defaults: dict[str, object] = {
        "hash": "placeholder",
        "action_type": ActionType.rollback,
        "target": "k8s:demo/leaky-service",
        "parameters": {"replicas": 1},
        "expected_effect": "Restore the deployment to a healthy replica count.",
        "rollback_hint": "kubectl scale --replicas=1 deployment/leaky-service",
        "signer": "planner",
        "signature": "placeholder",
        "expires_at": _ts(15),
    }
    defaults.update(overrides)
    return ActionIntent(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compute_intent_hash
# ---------------------------------------------------------------------------


class TestComputeIntentHash:
    def test_stability_same_inputs(self) -> None:
        """Identical inputs must always produce the same hash."""
        kwargs: dict[str, object] = {
            "action_type": "rollback",
            "target": "k8s:demo/leaky-service",
            "parameters": {"replicas": 1},
            "expected_effect": "Restore.",
            "rollback_hint": "kubectl scale --replicas=1",
        }
        h1 = compute_intent_hash(**kwargs)  # type: ignore[arg-type]
        h2 = compute_intent_hash(**kwargs)  # type: ignore[arg-type]
        assert h1 == h2

    def test_is_hex_string_length(self) -> None:
        h = compute_intent_hash(
            action_type="scale",
            target="k8s:demo/svc",
            parameters={},
            expected_effect="noop",
            rollback_hint="none",
        )
        # SHA-256 → 64 hex characters
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_action_type_changes_hash(self) -> None:
        base: dict[str, object] = {
            "target": "k8s:demo/svc",
            "parameters": {"k": "v"},
            "expected_effect": "e",
            "rollback_hint": "r",
        }
        h1 = compute_intent_hash(action_type="rollback", **base)  # type: ignore[arg-type]
        h2 = compute_intent_hash(action_type="scale", **base)  # type: ignore[arg-type]
        assert h1 != h2

    def test_different_target_changes_hash(self) -> None:
        base: dict[str, object] = {
            "action_type": "rollback",
            "parameters": {},
            "expected_effect": "e",
            "rollback_hint": "r",
        }
        h1 = compute_intent_hash(target="k8s:demo/svc-a", **base)  # type: ignore[arg-type]
        h2 = compute_intent_hash(target="k8s:demo/svc-b", **base)  # type: ignore[arg-type]
        assert h1 != h2

    def test_different_parameters_change_hash(self) -> None:
        base: dict[str, object] = {
            "action_type": "scale",
            "target": "k8s:demo/svc",
            "expected_effect": "e",
            "rollback_hint": "r",
        }
        h1 = compute_intent_hash(parameters={"replicas": 1}, **base)  # type: ignore[arg-type]
        h2 = compute_intent_hash(parameters={"replicas": 2}, **base)  # type: ignore[arg-type]
        assert h1 != h2

    def test_different_expected_effect_changes_hash(self) -> None:
        base: dict[str, object] = {
            "action_type": "scale",
            "target": "k8s:demo/svc",
            "parameters": {},
            "rollback_hint": "r",
        }
        h1 = compute_intent_hash(expected_effect="effect-a", **base)  # type: ignore[arg-type]
        h2 = compute_intent_hash(expected_effect="effect-b", **base)  # type: ignore[arg-type]
        assert h1 != h2

    def test_different_rollback_hint_changes_hash(self) -> None:
        base: dict[str, object] = {
            "action_type": "scale",
            "target": "k8s:demo/svc",
            "parameters": {},
            "expected_effect": "e",
        }
        h1 = compute_intent_hash(rollback_hint="hint-a", **base)  # type: ignore[arg-type]
        h2 = compute_intent_hash(rollback_hint="hint-b", **base)  # type: ignore[arg-type]
        assert h1 != h2


# ---------------------------------------------------------------------------
# Signer and Verifier — round-trip
# ---------------------------------------------------------------------------


@pytest.fixture()
def keypair_env(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Generate a fresh Ed25519 keypair and set environment variables."""
    private_pem, public_pem = generate_test_keypair()
    monkeypatch.setenv("PLANNER_SIGNING_KEY", private_pem)
    monkeypatch.setenv("PLANNER_VERIFY_KEY", public_pem)
    return private_pem, public_pem


class TestSignerVerifier:
    def test_round_trip(self, keypair_env: tuple[str, str]) -> None:
        """Sign an intent and successfully verify it."""
        signer = Signer()
        verifier = Verifier()

        intent = _make_intent()
        signed = signer.sign(intent)

        # hash and signature must be populated
        assert signed.hash != "placeholder"
        assert signed.signature != "placeholder"

        assert verifier.verify(signed) is True

    def test_sign_sets_correct_hash(self, keypair_env: tuple[str, str]) -> None:
        """The hash stored by sign() must equal compute_intent_hash()."""
        signer = Signer()
        intent = _make_intent()
        signed = signer.sign(intent)

        expected = compute_intent_hash(
            action_type=str(intent.action_type),
            target=intent.target,
            parameters=dict(intent.parameters),
            expected_effect=intent.expected_effect,
            rollback_hint=intent.rollback_hint,
        )
        assert signed.hash == expected

    def test_original_intent_not_mutated(self, keypair_env: tuple[str, str]) -> None:
        """sign() must return a new ActionIntent, not mutate the original."""
        signer = Signer()
        intent = _make_intent()
        signed = signer.sign(intent)

        assert intent.hash == "placeholder"
        assert intent.signature == "placeholder"
        assert signed is not intent

    def test_tamper_parameters_raises(self, keypair_env: tuple[str, str]) -> None:
        """Mutating parameters after signing must cause verify() to raise."""
        signer = Signer()
        verifier = Verifier()

        intent = _make_intent()
        signed = signer.sign(intent)

        # ActionIntent is frozen; use model_copy to simulate a tampered copy
        # that preserves the original signature but has different parameters.
        tampered = signed.model_copy(update={"parameters": {"replicas": 99}})

        with pytest.raises(IntentVerificationError):
            verifier.verify(tampered)

    def test_tamper_target_raises(self, keypair_env: tuple[str, str]) -> None:
        """Mutating target after signing must cause verify() to raise."""
        signer = Signer()
        verifier = Verifier()

        signed = signer.sign(_make_intent())
        tampered = signed.model_copy(update={"target": "k8s:prod/critical-service"})

        with pytest.raises(IntentVerificationError):
            verifier.verify(tampered)

    def test_tamper_signature_bytes_raises(self, keypair_env: tuple[str, str]) -> None:
        """Corrupting the base64 signature must cause verify() to raise."""
        signer = Signer()
        verifier = Verifier()

        signed = signer.sign(_make_intent())
        tampered = signed.model_copy(update={"signature": "AAAAAAAAAA=="})

        with pytest.raises(IntentVerificationError):
            verifier.verify(tampered)

    def test_wrong_public_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verifying with a *different* public key must raise."""
        private_pem_a, _ = generate_test_keypair()
        _, public_pem_b = generate_test_keypair()

        monkeypatch.setenv("PLANNER_SIGNING_KEY", private_pem_a)
        monkeypatch.setenv("PLANNER_VERIFY_KEY", public_pem_b)

        signer = Signer()
        verifier = Verifier()

        signed = signer.sign(_make_intent())

        with pytest.raises(IntentVerificationError):
            verifier.verify(signed)


# ---------------------------------------------------------------------------
# Missing env-var guard rails
# ---------------------------------------------------------------------------


class TestMissingEnvVars:
    def test_signer_raises_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PLANNER_SIGNING_KEY", raising=False)
        with pytest.raises(RuntimeError, match="PLANNER_SIGNING_KEY"):
            Signer()

    def test_verifier_raises_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PLANNER_VERIFY_KEY", raising=False)
        with pytest.raises(RuntimeError, match="PLANNER_VERIFY_KEY"):
            Verifier()
