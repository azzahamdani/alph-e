"""Investigator node — proposes/updates hypotheses and picks the next question.

MVP1 skeleton: increments ``investigation_attempts`` so the attempts cap
actually bites during graph tests. Real reasoning (LLM-backed hypothesis
generation) lands in WI-010.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.schemas import IncidentState, TimelineEvent


def investigator_node(state: IncidentState) -> dict[str, object]:
    now = datetime.now(UTC)
    timeline = [
        *state.timeline,
        TimelineEvent(
            ts=now,
            actor="orchestrator:investigator",
            event_type="investigator.tick",
        ),
    ]
    return {
        "investigation_attempts": state.investigation_attempts + 1,
        "timeline": timeline,
        "updated_at": now,
    }
