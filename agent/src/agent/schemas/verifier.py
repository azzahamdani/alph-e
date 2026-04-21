"""Verifier output — distinguishes implementation defects from diagnosis invalidation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class VerifierResultKind(StrEnum):
    """Two routing outcomes per arch doc's 'Key routing decisions' table."""

    passed = "passed"
    implementation_error = "implementation_error"
    diagnosis_invalidated = "diagnosis_invalidated"


class VerifierResult(BaseModel):
    """Structured verifier output. ``kind`` is how routing decides where to send it next."""

    model_config = ConfigDict(frozen=True)

    kind: VerifierResultKind
    checks_run: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    dry_run_output: str = ""
    reasoning: str = Field(
        default="",
        description="Why this kind was chosen; feeds the next node's prompt.",
    )
