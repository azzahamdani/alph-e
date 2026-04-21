"""End-to-end integration test: OOM alert traverses the orchestrator graph.

Exercises the pipeline:
    Intake (simulated) → Investigator → Planner → Coordinator

LLM calls are mocked — no real API key, no network, no cluster required.
The test is self-contained and deterministic.

Marking: ``integration`` — exercises full node wiring rather than individual units.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agent.orchestrator.nodes import (
    coordinator_node,
    investigator_node,
    planner_node,
)
from agent.orchestrator.nodes._models import InvestigatorOutput
from agent.schemas import (
    Action,
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
from agent.security.action_intent import Verifier, generate_test_keypair

# ---------------------------------------------------------------------------
# Module-level test keypair for ActionIntent signing (Planner → Coordinator).
# ---------------------------------------------------------------------------

_PRIVATE_PEM, _PUBLIC_PEM = generate_test_keypair()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2026, 4, 21, 14, 2, 17, tzinfo=UTC)


def _ts(offset_seconds: int = 0) -> datetime:
    return _BASE_TS + timedelta(seconds=offset_seconds)


def _apply_node_output(state: IncidentState, updates: dict[str, object]) -> IncidentState:
    """Thread a node's output dict back into IncidentState.

    Filters out keys that are not fields on IncidentState (e.g. ``remediation_plan``
    returned by the planner stub) so that ``model_copy`` never receives unknown fields.
    """
    valid_fields = set(IncidentState.model_fields.keys())
    filtered = {k: v for k, v in updates.items() if k in valid_fields}
    return state.model_copy(update=filtered)


# ---------------------------------------------------------------------------
# Canned fixtures
# ---------------------------------------------------------------------------

def _oom_alert() -> Alert:
    return Alert(
        source="alertmanager",
        raw_message="PodOOMKilled: leaky-service",
        service="leaky-service",
        severity=Severity.high,
        fired_at=_ts(),
        labels={
            "alertname": "PodOOMKilled",
            "namespace": "demo",
            "severity": "high",
            "service": "leaky-service",
            "pod": "leaky-service-66d7b8c4d7-abc12",
        },
    )


def _confirmed_hypothesis() -> Hypothesis:
    return Hypothesis(
        id="hyp_oom_1",
        text="Memory leak in leaky-service",
        score=0.9,
        status=HypothesisStatus.confirmed,
        created_at=_ts(),
    )


def _initial_state() -> IncidentState:
    """Construct the IncidentState that Intake would have produced."""
    incident_id = f"inc_{uuid.uuid4().hex[:8]}"
    now = _ts()
    return IncidentState(
        incident_id=incident_id,
        alert=_oom_alert(),
        phase=IncidentPhase.investigating,
        hypotheses=[_confirmed_hypothesis()],
        actions_taken=[],
        timeline=[
            TimelineEvent(
                ts=now,
                actor="orchestrator:intake",
                event_type="intake.accepted",
            )
        ],
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# LLM mock helpers
# ---------------------------------------------------------------------------

def _make_investigator_output(state: IncidentState) -> InvestigatorOutput:
    """Canned InvestigatorOutput: keep the confirmed hypothesis, no changes."""
    return InvestigatorOutput(
        hypotheses=list(state.hypotheses),
        current_focus_hypothesis_id="hyp_oom_1",
        reasoning=(
            "The confirmed hypothesis (memory leak in leaky-service, score=0.9) "
            "is the primary focus. OOM pattern is consistent with this diagnosis."
        ),
    )


def _make_planner_decision(**_kwargs: Any) -> object:
    """Canned _LLMPlannerDecision: scale to zero, high confidence."""
    # Import the private model to build the correct type.
    from agent.orchestrator.nodes.planner import _LLMPlannerDecision  # noqa: PLC0415

    return _LLMPlannerDecision(
        plan_id="plan_oom_scale_0",
        plan_type="scale",
        rationale="Scaling leaky-service to 0 stops the OOM loop immediately.",
        confidence=0.85,
        requires_human_approval=True,
        evidence_ids=[],
        target_services=["leaky-service"],
        target_repos=[],
        rollback_plan="Scale deployment back to 1 replica.",
        action_target="k8s:demo/leaky-service",
        action_parameters={"replicas": 0},
        expected_effect="Scale deployment to zero to stop the memory leak.",
        rollback_hint="Scale deployment back to 1 replica.",
        reasoning="Confirmed memory leak hypothesis drives scale-to-zero plan.",
    )


def _complete_typed_side_effect(
    _client: Any,
    *,
    system: str,
    messages: list[Any],
    output_model: type,
    max_retries: int = 1,
) -> object:
    """Route the mock complete_typed call to the right canned output."""
    name = output_model.__name__
    if name == "InvestigatorOutput":
        # We don't have access to `state` here, so build from scratch.
        from agent.orchestrator.nodes._models import InvestigatorOutput as IO  # noqa: PLC0415

        return IO(
            hypotheses=[_confirmed_hypothesis()],
            current_focus_hypothesis_id="hyp_oom_1",
            reasoning="Memory leak confirmed; focus on leaky-service.",
        )
    if name == "_LLMPlannerDecision":
        return _make_planner_decision()
    msg = f"Unexpected output_model in mock: {name!r}"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_oom_alert_traverses_investigator_planner_coordinator() -> None:
    """OOM alert travels Investigator → Planner → Coordinator and ends escalated.

    Acceptance criteria (from LAUNCH.md / X-01):
    - ``phase`` is ``escalated`` or ``resolved`` after the coordinator runs.
    - ``actions_taken`` is a non-empty list of Action objects.
    - ``timeline`` contains at least one event whose ``event_type`` starts
      with ``"coordinator."``.
    """
    os.environ["ANTHROPIC_API_KEY"] = "sk-test-dummy-key-for-mocking"
    os.environ["PLANNER_SIGNING_KEY"] = _PRIVATE_PEM
    os.environ["PLANNER_VERIFY_KEY"] = _PUBLIC_PEM

    mock_complete = AsyncMock(side_effect=_complete_typed_side_effect)

    with (
        patch("agent.orchestrator.nodes.investigator.complete_typed", mock_complete),
        patch("agent.orchestrator.nodes.planner.complete_typed", mock_complete),
    ):
        # -- Stage 0: initial state ------------------------------------------
        state = _initial_state()
        assert state.phase == IncidentPhase.investigating
        assert any(h.status == HypothesisStatus.confirmed for h in state.hypotheses)

        # -- Stage 1: Investigator node --------------------------------------
        inv_updates = await investigator_node(state)
        state = _apply_node_output(state, inv_updates)

        assert state.investigation_attempts == 1
        assert any(e.event_type == "investigator.tick" for e in state.timeline)

        # -- Stage 2: Routing check ------------------------------------------
        from agent.orchestrator.routing import route_after_investigator

        assert route_after_investigator(state) == "planner", (
            "Confirmed hypothesis must route to planner"
        )

        # -- Stage 3: Planner node -------------------------------------------
        plan_updates = await planner_node(state)
        state = _apply_node_output(state, plan_updates)

        assert state.phase == IncidentPhase.planning
        assert any(e.event_type == "planner.decided" for e in state.timeline)
        # Planner must have produced a signed ActionIntent for the scale plan.
        assert len(state.action_intents) >= 1, (
            "Planner must produce an ActionIntent for a scale plan"
        )

        # -- Stage 4: Coordinator node ---------------------------------------
        verifier = Verifier()
        from agent.orchestrator.nodes.coordinator import run_coordinator  # noqa: PLC0415

        coord_updates = await run_coordinator(state, verifier=verifier)
        state = _apply_node_output(state, coord_updates)

    # -- Assertions ----------------------------------------------------------
    assert state.phase in {IncidentPhase.escalated, IncidentPhase.resolved}, (
        f"Expected escalated or resolved, got {state.phase!r}"
    )

    coordinator_events = [
        e for e in state.timeline if e.event_type.startswith("coordinator.")
    ]
    assert coordinator_events, (
        f"Expected coordinator.* timeline events; got {[e.event_type for e in state.timeline]}"
    )

    assert len(state.actions_taken) >= 1, "actions_taken must be non-empty"
    assert all(isinstance(a, Action) for a in state.actions_taken)

    # State round-trips through JSON cleanly.
    restored = IncidentState.model_validate_json(state.model_dump_json())
    assert restored.phase == state.phase
    assert len(restored.timeline) == len(state.timeline)
    assert len(restored.actions_taken) == len(state.actions_taken)
