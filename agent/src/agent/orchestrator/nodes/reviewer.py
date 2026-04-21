"""Reviewer node — gates the PR before handoff to a human.

MVP1 skeleton: rubber-stamps whatever Verifier approves. Real review (policy
checks, diff lint, signer verification) lands in WI-011/WI-012.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.schemas import IncidentPhase, IncidentState, TimelineEvent


def reviewer_node(state: IncidentState) -> dict[str, object]:
    now = datetime.now(UTC)
    timeline = [
        *state.timeline,
        TimelineEvent(
            ts=now,
            actor="orchestrator:reviewer",
            event_type="reviewer.approved.skeleton",
        ),
    ]
    return {"phase": IncidentPhase.reviewing, "timeline": timeline, "updated_at": now}
