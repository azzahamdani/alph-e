"""Routing decisions as pure functions — one per non-obvious edge.

These functions never mutate state. They take a slice and return the name of
the next node (or a ``LangGraph`` sentinel). Keep them narrow: one hypothesis
per function, no composite decisions.

Source of truth for the edges: ``docs/devops-agent-architecture.md`` lines
204–213, 'Key routing decisions'.
"""

from __future__ import annotations

from typing import Final

from agent.schemas import (
    IncidentState,
    RemediationPlan,
    RemediationType,
    VerifierResult,
    VerifierResultKind,
)

MAX_INVESTIGATION_ATTEMPTS: Final[int] = 5
MAX_DEV_VERIFY_ITERATIONS: Final[int] = 3

# Node name constants — keep string literals out of the graph definition.
NODE_INTAKE: Final[str] = "intake"
NODE_INVESTIGATOR: Final[str] = "investigator"
NODE_COLLECTORS: Final[str] = "collectors"
NODE_PLANNER: Final[str] = "planner"
NODE_DEV: Final[str] = "dev"
NODE_VERIFIER: Final[str] = "verifier"
NODE_REVIEWER: Final[str] = "reviewer"
NODE_COORDINATOR: Final[str] = "coordinator"
NODE_ESCALATION: Final[str] = "escalation"


def route_after_investigator(state: IncidentState) -> str:
    """Investigator → Collectors, Planner, or Coordinator (escalation)."""
    if state.investigation_attempts >= MAX_INVESTIGATION_ATTEMPTS:
        return NODE_COORDINATOR
    if any(h.status == "confirmed" for h in state.hypotheses):
        return NODE_PLANNER
    return NODE_COLLECTORS


def route_after_planner(plan: RemediationPlan) -> str:
    """Planner → Dev for PRs; Coordinator for ops/none; nowhere else."""
    if plan.type == RemediationType.pr:
        return NODE_DEV
    # ``none`` is a legitimate outcome — escalate rather than force a PR.
    return NODE_COORDINATOR


def route_after_verifier(result: VerifierResult) -> str:
    """Verifier outcomes per arch doc table: impl err → Dev, diag inv → Investigator."""
    match result.kind:
        case VerifierResultKind.passed:
            return NODE_REVIEWER
        case VerifierResultKind.implementation_error:
            return NODE_DEV
        case VerifierResultKind.diagnosis_invalidated:
            return NODE_INVESTIGATOR


def route_after_reviewer(*, challenges_root_cause: bool) -> str:
    """Reviewer: changes on the fix → Dev; challenge to diagnosis → Investigator."""
    return NODE_INVESTIGATOR if challenges_root_cause else NODE_DEV
