"""Collectors dispatch node — fans out to Go collector HTTP services.

MVP1 skeleton: no real HTTP calls, just records a timeline event. Real
dispatch (WI-009) maps ``state.current_focus_hypothesis_id`` to a
``CollectorInput`` and POSTs to the appropriate ``collectors/*`` service.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.schemas import IncidentState, TimelineEvent


def collectors_node(state: IncidentState) -> dict[str, object]:
    now = datetime.now(UTC)
    timeline = [
        *state.timeline,
        TimelineEvent(
            ts=now,
            actor="orchestrator:collectors",
            event_type="collectors.dispatch.skeleton",
        ),
    ]
    return {"timeline": timeline, "updated_at": now}
