"""Planner node — produces a ``RemediationPlan``.

MVP1 skeleton: always returns ``type=none`` so the graph exercises the
escalation path end-to-end without any production mutations.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.schemas import (
    IncidentPhase,
    IncidentState,
    RemediationPlan,
    RemediationType,
    TimelineEvent,
)


def planner_node(state: IncidentState) -> dict[str, object]:
    now = datetime.now(UTC)
    plan = RemediationPlan(
        id=f"plan_{state.incident_id}",
        type=RemediationType.none,
        rationale=(
            "MVP1 skeleton: Planner stub always returns type=none so the graph "
            "escalates via Coordinator instead of forcing a PR."
        ),
        confidence=0.0,
        requires_human_approval=True,
    )
    timeline = [
        *state.timeline,
        TimelineEvent(
            ts=now,
            actor="orchestrator:planner",
            event_type="planner.decided",
            ref_id=plan.id,
        ),
    ]
    return {
        "phase": IncidentPhase.planning,
        "timeline": timeline,
        "updated_at": now,
        "remediation_plan": plan,
    }
