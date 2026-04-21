"""Collector contract — CollectorInput / CollectorOutput, verbatim from the arch doc."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from agent.schemas.evidence import EvidenceRef


class TimeRange(BaseModel):
    """Half-open [start, end) range used to bound collector queries."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime


class EnvironmentFingerprint(BaseModel):
    """Identifies the cluster / account / region / revision the incident is about.

    Included in the collector cache key so findings from a different environment
    never get reused across incidents.
    """

    model_config = ConfigDict(frozen=True)

    cluster: str
    account: str
    region: str
    deploy_revision: str
    rollout_generation: str


class Finding(BaseModel):
    """A single refined collector output — one question, one answer."""

    id: str
    collector_name: str
    question: str
    summary: str = Field(..., description="Human-readable one-line summary; feeds the orchestrator.")
    evidence_id: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    suggested_followups: list[str] = Field(default_factory=list)
    created_at: datetime


class CollectorInput(BaseModel):
    """What the orchestrator sends to a collector.

    Collectors are pure-ish functions; each call is a fresh context.
    ``max_internal_iterations`` caps the collector's own tool-use loop so it
    can't burn its context window chasing a dead hypothesis.
    """

    model_config = ConfigDict(frozen=True)

    incident_id: str
    question: str
    hypothesis_id: str
    time_range: TimeRange
    scope_services: list[str]
    environment_fingerprint: EnvironmentFingerprint
    max_internal_iterations: int = Field(default=5, ge=1, le=10)


class CollectorOutput(BaseModel):
    """What the orchestrator receives back from a collector."""

    finding: Finding
    evidence: EvidenceRef
    tool_calls_used: int = Field(..., ge=0)
    tokens_used: int = Field(..., ge=0)
