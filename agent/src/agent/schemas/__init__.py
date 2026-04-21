"""Pydantic v2 schemas — the typed spine of the runtime.

Source of truth: ``docs/diagrams/state-schema.mmd``. Every box in the class
diagram has a model here; every enum likewise.
"""

from agent.schemas.collector import (
    CollectorInput,
    CollectorOutput,
    EnvironmentFingerprint,
    Finding,
    TimeRange,
)
from agent.schemas.escalation import BlockedReport, EscalationPackage
from agent.schemas.evidence import EvidenceRef
from agent.schemas.incident import (
    Action,
    ActionIntent,
    ActionStatus,
    ActionType,
    Alert,
    Hypothesis,
    HypothesisStatus,
    IncidentPhase,
    IncidentState,
    Severity,
    TimelineEvent,
)
from agent.schemas.remediation import (
    FileChange,
    FixProposal,
    RemediationPlan,
    RemediationType,
)
from agent.schemas.verifier import VerifierResult, VerifierResultKind

__all__ = [
    "Action",
    "ActionIntent",
    "ActionStatus",
    "ActionType",
    "Alert",
    "BlockedReport",
    "CollectorInput",
    "CollectorOutput",
    "EnvironmentFingerprint",
    "EscalationPackage",
    "EvidenceRef",
    "FileChange",
    "Finding",
    "FixProposal",
    "Hypothesis",
    "HypothesisStatus",
    "IncidentPhase",
    "IncidentState",
    "RemediationPlan",
    "RemediationType",
    "Severity",
    "TimeRange",
    "TimelineEvent",
    "VerifierResult",
    "VerifierResultKind",
]
