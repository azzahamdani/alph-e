"""Routing edges — the non-obvious ones from the arch doc table.

These functions are pure, so the tests are pure too. The graph compile is
exercised separately in ``test_graph_compile.py`` to keep failures isolated.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.orchestrator import routing
from agent.schemas import (
    Alert,
    Hypothesis,
    HypothesisStatus,
    IncidentPhase,
    IncidentState,
    RemediationPlan,
    RemediationType,
    Severity,
    VerifierResult,
    VerifierResultKind,
)


def _state(
    *,
    attempts: int = 0,
    hypotheses: list[Hypothesis] | None = None,
) -> IncidentState:
    now = datetime(2026, 4, 21, 14, 0, tzinfo=UTC)
    return IncidentState(
        incident_id="inc_test",
        alert=Alert(
            source="test",
            raw_message="x",
            service="demo",
            severity=Severity.high,
            fired_at=now,
        ),
        hypotheses=hypotheses or [],
        investigation_attempts=attempts,
        phase=IncidentPhase.investigating,
        created_at=now,
        updated_at=now,
    )


def _hyp(status: HypothesisStatus) -> Hypothesis:
    return Hypothesis(
        id="hyp_1",
        text="t",
        score=0.9,
        status=status,
        created_at=datetime(2026, 4, 21, 14, 0, tzinfo=UTC),
    )


def test_investigator_routes_to_collectors_when_no_confirmed_hypothesis() -> None:
    assert routing.route_after_investigator(_state()) == routing.NODE_COLLECTORS


def test_investigator_routes_to_planner_on_confirmed_hypothesis() -> None:
    state = _state(hypotheses=[_hyp(HypothesisStatus.confirmed)])
    assert routing.route_after_investigator(state) == routing.NODE_PLANNER


def test_investigator_escalates_when_attempts_exhausted() -> None:
    state = _state(attempts=routing.MAX_INVESTIGATION_ATTEMPTS)
    assert routing.route_after_investigator(state) == routing.NODE_COORDINATOR


def test_planner_pr_routes_to_dev() -> None:
    plan = RemediationPlan(id="p", type=RemediationType.pr, rationale="r")
    assert routing.route_after_planner(plan) == routing.NODE_DEV


def test_planner_non_pr_routes_to_coordinator() -> None:
    for kind in (
        RemediationType.none,
        RemediationType.rollback,
        RemediationType.scale,
        RemediationType.flag_flip,
        RemediationType.runbook,
    ):
        plan = RemediationPlan(id="p", type=kind, rationale="r")
        assert routing.route_after_planner(plan) == routing.NODE_COORDINATOR


def test_verifier_implementation_error_returns_to_dev() -> None:
    result = VerifierResult(kind=VerifierResultKind.implementation_error)
    assert routing.route_after_verifier(result) == routing.NODE_DEV


def test_verifier_diagnosis_invalidated_returns_to_investigator() -> None:
    result = VerifierResult(kind=VerifierResultKind.diagnosis_invalidated)
    assert routing.route_after_verifier(result) == routing.NODE_INVESTIGATOR


def test_verifier_passed_routes_to_reviewer() -> None:
    result = VerifierResult(kind=VerifierResultKind.passed)
    assert routing.route_after_verifier(result) == routing.NODE_REVIEWER


def test_reviewer_changes_on_fix_route_back_to_dev() -> None:
    assert routing.route_after_reviewer(challenges_root_cause=False) == routing.NODE_DEV


def test_reviewer_challenge_root_cause_routes_to_investigator() -> None:
    assert (
        routing.route_after_reviewer(challenges_root_cause=True)
        == routing.NODE_INVESTIGATOR
    )
