"""Remediation plan and fix-proposal types."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RemediationType(StrEnum):
    pr = "pr"
    rollback = "rollback"
    scale = "scale"
    flag_flip = "flag_flip"
    runbook = "runbook"
    none = "none"


class RemediationPlan(BaseModel):
    """Pre-flight remediation decision. ``type=none`` is a valid outcome."""

    model_config = ConfigDict(frozen=True)

    id: str
    type: RemediationType
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    target_repos: list[str] = Field(default_factory=list)
    target_services: list[str] = Field(default_factory=list)
    rollback_plan: str = Field(default="")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    requires_human_approval: bool = Field(
        default=True,
        description=(
            "Default True for anything that mutates production. "
            "Only pr and none are safe to default-False."
        ),
    )


class FileChange(BaseModel):
    """A single file in a FixProposal's diff."""

    model_config = ConfigDict(frozen=True)

    repo: str
    path: str
    diff: str


class FixProposal(BaseModel):
    """What the Dev agent produces for ``type=pr`` plans."""

    model_config = ConfigDict(frozen=True)

    id: str
    plan_id: str
    branch_name: str
    changes: list[FileChange]
    commit_message: str
    pr_body: str
