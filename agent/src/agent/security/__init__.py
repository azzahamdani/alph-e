"""Security utilities for the DevOps agent.

Exports the core signing / verification API so callers can do::

    from agent.security import Signer, Verifier, IntentVerificationError, compute_intent_hash
"""

from agent.security.action_intent import (
    IntentVerificationError,
    Signer,
    Verifier,
    compute_intent_hash,
    generate_test_keypair,
)

__all__ = [
    "IntentVerificationError",
    "Signer",
    "Verifier",
    "compute_intent_hash",
    "generate_test_keypair",
]
