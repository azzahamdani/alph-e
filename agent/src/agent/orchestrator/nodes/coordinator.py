"""Coordinator node — closes incidents and owns escalation.

MVP1 skeleton: marks the incident ``escalated`` with a no-op acknowledgement
trail. Real Coordinator (operational actions, ``ActionIntent`` verification)
lands in WI-012.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.schemas import IncidentPhase, IncidentState, TimelineEvent


def coordinator_node(state: IncidentState) -> dict[str, object]:
    now = datetime.now(UTC)
    timeline = [
        *state.timeline,
        TimelineEvent(
            ts=now,
            actor="orchestrator:coordinator",
            event_type="coordinator.acknowledged.skeleton",
        ),
    ]
    return {"phase": IncidentPhase.escalated, "timeline": timeline, "updated_at": now}
