"""Every schema must survive a JSON round-trip.

Catches the easy mistakes: enum values renamed, required fields forgotten in
defaults, datetime parsing drift, frozen models rejecting valid input.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent.schemas import (
    Action,
    ActionIntent,
    ActionStatus,
    ActionType,
    Alert,
    BlockedReport,
    CollectorInput,
    CollectorOutput,
    EnvironmentFingerprint,
    EscalationPackage,
    EvidenceRef,
    FileChange,
    Finding,
    FixProposal,
    Hypothesis,
    HypothesisStatus,
    IncidentPhase,
    IncidentState,
    RemediationPlan,
    RemediationType,
    Severity,
    TimeRange,
    TimelineEvent,
    VerifierResult,
    VerifierResultKind,
)


def _ts(offset_minutes: int = 0) -> datetime:
    return datetime(2026, 4, 21, 14, 0, tzinfo=UTC) + timedelta(minutes=offset_minutes)


def _evidence_ref() -> EvidenceRef:
    return EvidenceRef(
        evidence_id="ev_8f2a",
        storage_uri="s3://incidents/ev_8f2a.jsonl",
        content_type="application/x-ndjson",
        size_bytes=4_821_334,
        expires_at=_ts(30 * 24 * 60),
    )


def _finding() -> Finding:
    return Finding(
        id="f_1",
        collector_name="loki",
        question="Is db-primary showing connection errors in the last 15m?",
        summary="847 'connection refused' errors starting at 14:02:17",
        evidence_id="ev_8f2a",
        confidence=0.92,
        created_at=_ts(),
    )


def _roundtrip[T](obj: T) -> T:  # type: ignore[valid-type]
    cls = type(obj)
    return cls.model_validate_json(obj.model_dump_json())  # type: ignore[attr-defined]


def test_evidence_ref_roundtrip() -> None:
    ref = _evidence_ref()
    assert _roundtrip(ref) == ref


def test_collector_input_output_roundtrip() -> None:
    fingerprint = EnvironmentFingerprint(
        cluster="prod-eu-west-1",
        account="123456789012",
        region="eu-west-1",
        deploy_revision="api@v2.14.3",
        rollout_generation="api-7f9a",
    )
    input_ = CollectorInput(
        incident_id="inc_2a91",
        question="Is db-primary showing connection errors in the last 15m?",
        hypothesis_id="hyp_3",
        time_range=TimeRange(start=_ts(), end=_ts(15)),
        scope_services=["db-primary"],
        environment_fingerprint=fingerprint,
    )
    output = CollectorOutput(
        finding=_finding(),
        evidence=_evidence_ref(),
        tool_calls_used=3,
        tokens_used=1842,
    )
    assert _roundtrip(input_) == input_
    assert _roundtrip(output) == output
    assert input_.max_internal_iterations == 5


def test_incident_state_roundtrip() -> None:
    alert = Alert(
        source="alertmanager",
        raw_message="PodOOMKilled",
        service="leaky-service",
        severity=Severity.critical,
        fired_at=_ts(),
        labels={"alertname": "PodOOMKilled", "namespace": "demo"},
    )
    hypothesis = Hypothesis(
        id="hyp_1",
        text="leaky-service is OOMKilled because memory_leak is accumulating",
        score=0.8,
        status=HypothesisStatus.open,
        created_at=_ts(),
    )
    intent = ActionIntent(
        hash="deadbeef",
        action_type=ActionType.rollback,
        target="k8s:demo/leaky-service",
        parameters={"replicas": 0},
        expected_effect="Stop the bleeding.",
        rollback_hint="kubectl scale --replicas=1 deployment/leaky-service",
        signer="planner",
        signature="sig_abc",
        expires_at=_ts(15),
    )
    action = Action(
        id="act_1",
        type=ActionType.no_op,
        description="skeleton acknowledgement",
        status=ActionStatus.succeeded,
        intent_hash=intent.hash,
        executed_at=_ts(1),
    )
    state = IncidentState(
        incident_id="inc_2a91",
        alert=alert,
        hypotheses=[hypothesis],
        findings=[_finding()],
        timeline=[
            TimelineEvent(
                ts=_ts(), actor="orchestrator:intake", event_type="intake.accepted"
            )
        ],
        actions_taken=[action],
        action_intents=[intent],
        phase=IncidentPhase.intake,
        created_at=_ts(),
        updated_at=_ts(),
    )
    restored = _roundtrip(state)
    assert restored == state
    assert restored.phase is IncidentPhase.intake


def test_remediation_roundtrip() -> None:
    plan = RemediationPlan(
        id="plan_1",
        type=RemediationType.none,
        rationale="No actionable remediation; escalate.",
        confidence=0.0,
    )
    proposal = FixProposal(
        id="prop_1",
        plan_id=plan.id,
        branch_name="fix/leaky-service",
        changes=[FileChange(repo="demo-app", path="app.py", diff="@@ -1 +1 @@\n-foo\n+bar\n")],
        commit_message="fix: stop the leak",
        pr_body="Fix the leak. Root cause: hyp_1.",
    )
    assert _roundtrip(plan) == plan
    assert _roundtrip(proposal) == proposal


def test_verifier_and_escalation_roundtrip() -> None:
    verifier = VerifierResult(
        kind=VerifierResultKind.diagnosis_invalidated,
        checks_run=["dry_run"],
        failures=["root-cause observation no longer holds"],
        dry_run_output="ok",
        reasoning="the observation has drifted",
    )
    package = EscalationPackage(
        incident_id="inc_2a91",
        hypotheses_considered=[
            Hypothesis(
                id="hyp_1",
                text="memory leak",
                score=0.8,
                status=HypothesisStatus.abandoned,
                created_at=_ts(),
            )
        ],
        key_findings=[_finding()],
        current_working_theory="needs human judgement",
        suggested_next_steps=["check recent deploys"],
    )
    blocked = BlockedReport(
        work_item_id="WI-010",
        what_was_tried=["three prompts"],
        what_failed=["LLM refused to pick a single hypothesis"],
        decision_needed="product decision on ambiguity handling",
        partial_artifacts=[],
    )
    assert _roundtrip(verifier) == verifier
    assert _roundtrip(package) == package
    assert _roundtrip(blocked) == blocked
