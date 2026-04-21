"""Escalation + build-fleet blocked-report shapes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agent.schemas.collector import Finding
from agent.schemas.incident import Hypothesis
from agent.schemas.remediation import RemediationPlan


class EscalationPackage(BaseModel):
    """Structured handoff to an on-call engineer. Never 'I failed N times.'

    See arch doc 'What the EscalationPackage contains'.
    """

    model_config = ConfigDict(frozen=True)

    incident_id: str
    hypotheses_considered: list[Hypothesis]
    key_findings: list[Finding]
    attempts: list[RemediationPlan] = Field(default_factory=list)
    attempt_failure_reasons: list[str] = Field(default_factory=list)
    current_working_theory: str
    suggested_next_steps: list[str] = Field(default_factory=list)


class BlockedReport(BaseModel):
    """What a build-fleet specialist produces when it can't finish within max_iterations."""

    model_config = ConfigDict(frozen=True)

    work_item_id: str
    what_was_tried: list[str]
    what_failed: list[str]
    decision_needed: str
    partial_artifacts: list[str] = Field(
        default_factory=list,
        description="Paths or refs to partial work; reviewer may resume from these.",
    )
