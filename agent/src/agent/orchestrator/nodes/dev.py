"""Dev node — produces a ``FixProposal`` for ``type=pr`` plans.

MVP1 skeleton: never called under the always-``type=none`` planner stub, but
present so the graph compiles and routing tests can traverse it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.schemas import IncidentPhase, IncidentState, TimelineEvent


def dev_node(state: IncidentState) -> dict[str, object]:
    now = datetime.now(UTC)
    timeline = [
        *state.timeline,
        TimelineEvent(
            ts=now,
            actor="orchestrator:dev",
            event_type="dev.proposal.skeleton",
        ),
    ]
    return {"phase": IncidentPhase.fixing, "timeline": timeline, "updated_at": now}
