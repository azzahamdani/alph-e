"""IncidentState and its immediate children — the durable state container."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agent.schemas.collector import Finding


class Severity(StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class HypothesisStatus(StrEnum):
    open = "open"
    confirmed = "confirmed"
    refuted = "refuted"
    abandoned = "abandoned"


class IncidentPhase(StrEnum):
    intake = "intake"
    investigating = "investigating"
    planning = "planning"
    fixing = "fixing"
    verifying = "verifying"
    reviewing = "reviewing"
    escalated = "escalated"
    resolved = "resolved"


class ActionType(StrEnum):
    query = "query"
    fix_attempt = "fix_attempt"
    rollback = "rollback"
    scale = "scale"
    flag_flip = "flag_flip"
    notification = "notification"
    no_op = "no_op"


class ActionStatus(StrEnum):
    pending = "pending"
    executing = "executing"
    succeeded = "succeeded"
    failed = "failed"


class Alert(BaseModel):
    """The initial signal — typically parsed from an Alertmanager webhook."""

    source: str = Field(..., description="e.g. 'alertmanager', 'slack', 'manual'.")
    raw_message: str
    service: str
    severity: Severity
    fired_at: datetime
    labels: dict[str, str] = Field(default_factory=dict)


class Hypothesis(BaseModel):
    """A candidate root cause; scored and annotated with evidence over time."""

    id: str
    text: str
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    status: HypothesisStatus = HypothesisStatus.open
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    refuting_evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class TimelineEvent(BaseModel):
    """Audit trail entry: who-ran-what-when."""

    ts: datetime
    actor: str = Field(..., description="Node name, collector name, or 'human:<username>'.")
    event_type: str
    ref_id: str | None = None


class ActionIntent(BaseModel):
    """A typed, signed description of a mutation the Coordinator may execute.

    The Planner creates and signs; the Coordinator verifies and runs. Human
    approval binds to ``hash``; if the plan changes, approval is invalidated.
    See arch doc's 'Safety contract for operational actions'.
    """

    model_config = ConfigDict(frozen=True)

    hash: str = Field(..., description="Stable hash over target + parameters + expected effect.")
    action_type: ActionType
    target: str = Field(..., description="Fully-qualified target, e.g. 'k8s:demo/leaky-service'.")
    parameters: dict[str, str | int | bool] = Field(default_factory=dict)
    expected_effect: str
    rollback_hint: str
    signer: str = Field(..., description="Identity that produced this intent; Planner in MVP1.")
    signature: str
    approval_status: str = Field(
        default="pending",
        description="pending | granted | invalidated | rejected",
    )
    approved_by: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime = Field(
        ..., description="Ceiling on approval validity (default 15m per arch doc)."
    )


class Action(BaseModel):
    """An actually-attempted (or executed) action; refers to a signed ActionIntent."""

    id: str
    type: ActionType
    description: str
    status: ActionStatus
    intent_hash: str | None = Field(
        default=None,
        description="Binds this Action to the ActionIntent that authorised it.",
    )
    ref_id: str | None = None
    executed_at: datetime | None = None


class IncidentState(BaseModel):
    """The only durable container. Every other node receives a slice of this."""

    incident_id: str
    alert: Alert
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    actions_taken: list[Action] = Field(default_factory=list)
    action_intents: list[ActionIntent] = Field(default_factory=list)
    services_touched: list[str] = Field(default_factory=list)
    current_focus_hypothesis_id: str | None = None
    investigation_attempts: int = Field(default=0, ge=0)
    phase: IncidentPhase = IncidentPhase.intake
    created_at: datetime
    updated_at: datetime
