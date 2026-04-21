"""Intake node — normalises the seed ``IncidentState`` and advances the phase.

For MVP1 the webhook already produces an ``IncidentState`` in ``phase=intake``,
so this node is mostly a phase transition + audit trail entry.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.schemas import IncidentPhase, IncidentState, TimelineEvent


def intake_node(state: IncidentState) -> dict[str, object]:
    """Advance the incident from ``intake`` to ``investigating``."""
    now = datetime.now(UTC)
    timeline = [
        *state.timeline,
        TimelineEvent(ts=now, actor="orchestrator:intake", event_type="intake.accepted"),
    ]
    return {
        "phase": IncidentPhase.investigating,
        "timeline": timeline,
        "updated_at": now,
    }
