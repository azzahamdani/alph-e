"""ActionIntent signing and verification using Ed25519.

Security contract (arch doc §'Safety contract for operational actions'):
  - The Planner creates an ActionIntent and signs it with its private key.
  - The Coordinator verifies the signature using the Planner's public key
    before executing any remediation action.
  - Human approval binds to ``ActionIntent.hash``; mutating any field
    invalidates approval.

Key material is sourced from environment variables only — never from code.
The private key is never logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

from agent.schemas.incident import ActionIntent


class IntentVerificationError(Exception):
    """Raised when an ActionIntent fails signature verification."""


def compute_intent_hash(
    *,
    action_type: str,
    target: str,
    parameters: dict[str, str | int | bool],
    expected_effect: str,
    rollback_hint: str,
) -> str:
    """Return a hex SHA-256 digest over a canonical JSON representation.

    Canonical form: sorted keys, no whitespace separators.  Stable across
    Python versions and runs.  Covers every field that defines *what* the
    action does so that mutating any of them invalidates an existing signature.
    """
    payload: dict[str, object] = {
        "action_type": action_type,
        "expected_effect": expected_effect,
        "parameters": parameters,
        "rollback_hint": rollback_hint,
        "target": target,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _load_private_key(pem: str) -> Ed25519PrivateKey:
    key = load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("PLANNER_SIGNING_KEY must be an Ed25519 private key")
    return key


def _load_public_key(pem: str) -> Ed25519PublicKey:
    key = load_pem_public_key(pem.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("PLANNER_VERIFY_KEY must be an Ed25519 public key")
    return key


class Signer:
    """Signs ActionIntents using an Ed25519 private key.

    The private key PEM is read from the ``PLANNER_SIGNING_KEY`` environment
    variable.  It is held in memory as an opaque key object and is never
    serialised, stringified, or logged.
    """

    _PRIVATE_KEY_ENV = "PLANNER_SIGNING_KEY"

    def __init__(self) -> None:
        pem = os.environ.get(self._PRIVATE_KEY_ENV)
        if not pem:
            raise RuntimeError(
                f"Environment variable {self._PRIVATE_KEY_ENV!r} is not set. "
                "Provide a PEM-encoded Ed25519 private key."
            )
        self._private_key: Ed25519PrivateKey = _load_private_key(pem)

    def sign(self, intent: ActionIntent) -> ActionIntent:
        """Return a copy of *intent* with ``hash`` and ``signature`` populated.

        The hash is recomputed from the intent's fields; the signature covers
        the hash bytes so the recipient can verify without re-hashing.
        """
        intent_hash = compute_intent_hash(
            action_type=str(intent.action_type),
            target=intent.target,
            parameters=dict(intent.parameters),
            expected_effect=intent.expected_effect,
            rollback_hint=intent.rollback_hint,
        )
        raw_signature = self._private_key.sign(intent_hash.encode())
        signature_b64 = base64.b64encode(raw_signature).decode()
        # ActionIntent is frozen — use model_copy to produce a new instance.
        return intent.model_copy(update={"hash": intent_hash, "signature": signature_b64})


class Verifier:
    """Verifies ActionIntent signatures using an Ed25519 public key.

    The public key PEM is read from the ``PLANNER_VERIFY_KEY`` environment
    variable.  This is intentionally a *separate* identity from the Signer so
    that the Coordinator can verify without access to the Planner private key.
    """

    _PUBLIC_KEY_ENV = "PLANNER_VERIFY_KEY"

    def __init__(self) -> None:
        pem = os.environ.get(self._PUBLIC_KEY_ENV)
        if not pem:
            raise RuntimeError(
                f"Environment variable {self._PUBLIC_KEY_ENV!r} is not set. "
                "Provide a PEM-encoded Ed25519 public key."
            )
        self._public_key: Ed25519PublicKey = _load_public_key(pem)

    def verify(self, intent: ActionIntent) -> bool:
        """Return ``True`` if *intent* has a valid signature, else raise.

        Raises:
            IntentVerificationError: if the signature does not match or the
                hash does not match the intent's fields.
        """
        # Recompute the hash from the intent's authoritative fields.
        expected_hash = compute_intent_hash(
            action_type=str(intent.action_type),
            target=intent.target,
            parameters=dict(intent.parameters),
            expected_effect=intent.expected_effect,
            rollback_hint=intent.rollback_hint,
        )
        if intent.hash != expected_hash:
            raise IntentVerificationError(
                f"Intent hash mismatch: stored={intent.hash!r}, "
                f"computed={expected_hash!r}. The intent may have been tampered with."
            )
        try:
            raw_signature = base64.b64decode(intent.signature)
            self._public_key.verify(raw_signature, intent.hash.encode())
        except Exception as exc:
            raise IntentVerificationError(
                f"Ed25519 signature verification failed: {exc}"
            ) from exc
        return True


def generate_test_keypair() -> tuple[str, str]:
    """Generate an ephemeral Ed25519 keypair for use in tests.

    Returns:
        A ``(private_pem, public_pem)`` tuple.  Both are PEM-encoded strings.

    Warning:
        This function is intended for testing only.  Keys generated here are
        ephemeral and must never be used in production.
    """
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem
