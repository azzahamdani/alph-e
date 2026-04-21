"""Verifier node — returns a typed ``VerifierResult``.

MVP1 skeleton: always ``passed``. Real verification (dry-run, static checks)
lands in WI-011.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.schemas import (
    IncidentPhase,
    IncidentState,
    TimelineEvent,
    VerifierResult,
    VerifierResultKind,
)


def verifier_node(state: IncidentState) -> dict[str, object]:
    now = datetime.now(UTC)
    result = VerifierResult(
        kind=VerifierResultKind.passed,
        checks_run=["skeleton.noop"],
        reasoning="MVP1 skeleton: always passes; real checks live in WI-011.",
    )
    timeline = [
        *state.timeline,
        TimelineEvent(
            ts=now,
            actor="orchestrator:verifier",
            event_type="verifier.result",
        ),
    ]
    return {
        "phase": IncidentPhase.verifying,
        "timeline": timeline,
        "updated_at": now,
        "verifier_result": result,
    }
