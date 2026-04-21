"""Unit tests for the Coordinator node (B-07).

Covers:
- Signature verification failure → FAIL CLOSED, escalate.
- Preflight: diagnosis_invalidated → investigating phase.
- Preflight: parameter_drift → planning phase.
- Preflight: already_resolved → resolved phase with no_op action.
- Idempotent retry: second call with same intent hash skips execution.
- Partial-failure compensation path → escalated with rollback action recorded.
- EscalationPackage builds and serialises to JSON without error.
- type=none / no-intent path → escalated.

No LLM calls.  The coordinator is a deterministic state machine with no
external I/O in MVP1 dry-run mode.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from agent.orchestrator.coordinator.escalation import build_escalation_package
from agent.orchestrator.coordinator.exec import ExecutionRecord, IdempotentExecutor
from agent.orchestrator.coordinator.preflight import PreflightOutcome, check_preflight
from agent.orchestrator.nodes.coordinator import coordinator_node, run_coordinator
from agent.schemas import (
    Action,
    ActionIntent,
    ActionStatus,
    ActionType,
    Alert,
    Hypothesis,
    HypothesisStatus,
    IncidentPhase,
    IncidentState,
    Severity,
)
from agent.security.action_intent import (
    IntentVerificationError,
    Signer,
    Verifier,
    generate_test_keypair,
)

# ---------------------------------------------------------------------------
# Module-level test keypair — generated once, reused across all tests.
# ---------------------------------------------------------------------------

_PRIVATE_PEM, _PUBLIC_PEM = generate_test_keypair()

_NOW = datetime(2026, 4, 21, 14, 0, tzinfo=UTC)
_EXPIRES = _NOW + timedelta(minutes=15)


def _make_signer() -> Signer:
    os.environ["PLANNER_SIGNING_KEY"] = _PRIVATE_PEM
    return Signer()


def _make_verifier() -> Verifier:
    os.environ["PLANNER_VERIFY_KEY"] = _PUBLIC_PEM
    return Verifier()


# ---------------------------------------------------------------------------
# State/entity factories
# ---------------------------------------------------------------------------


def _alert() -> Alert:
    return Alert(
        source="test",
        raw_message="PodOOMKilled: leaky-service",
        service="leaky-service",
        severity=Severity.high,
        fired_at=_NOW,
    )


def _state(
    *,
    hypotheses: list[Hypothesis] | None = None,
    intents: list[ActionIntent] | None = None,
    actions: list[Action] | None = None,
    phase: IncidentPhase = IncidentPhase.fixing,
) -> IncidentState:
    return IncidentState(
        incident_id="inc_test",
        alert=_alert(),
        hypotheses=hypotheses or [],
        action_intents=intents or [],
        actions_taken=actions or [],
        phase=phase,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _confirmed_hyp(hyp_id: str = "hyp_1") -> Hypothesis:
    return Hypothesis(
        id=hyp_id,
        text="Memory leak in leaky-service",
        score=0.9,
        status=HypothesisStatus.confirmed,
        created_at=_NOW,
    )


def _open_hyp(hyp_id: str = "hyp_2") -> Hypothesis:
    return Hypothesis(
        id=hyp_id,
        text="Network saturation",
        score=0.4,
        status=HypothesisStatus.open,
        created_at=_NOW,
    )


def _refuted_hyp(hyp_id: str = "hyp_3") -> Hypothesis:
    return Hypothesis(
        id=hyp_id,
        text="Disk pressure",
        score=0.1,
        status=HypothesisStatus.refuted,
        created_at=_NOW,
    )


def _make_intent(*, bad_signature: bool = False) -> ActionIntent:
    """Build a validly-signed ActionIntent using the Ed25519 Signer."""
    base = ActionIntent(
        hash="placeholder",
        action_type=ActionType.scale,
        target="k8s:demo/leaky-service",
        parameters={"replicas": 0},
        expected_effect="Scale deployment to zero to stop the leak.",
        rollback_hint="Scale deployment back to 1 replica.",
        signer="orchestrator:planner",
        signature="placeholder",
        expires_at=_EXPIRES,
    )
    signed = _make_signer().sign(base)
    if bad_signature:
        # Tamper with the signature without changing the hash.
        return signed.model_copy(update={"signature": "BADSIG000000"})
    return signed


# ---------------------------------------------------------------------------
# Verifier unit tests
# ---------------------------------------------------------------------------


class TestVerifier:
    def test_valid_signature_passes(self) -> None:
        intent = _make_intent()
        _make_verifier().verify(intent)  # must not raise

    def test_bad_signature_raises(self) -> None:
        intent = _make_intent(bad_signature=True)
        with pytest.raises(IntentVerificationError, match="Ed25519 signature verification failed"):
            _make_verifier().verify(intent)

    def test_empty_signature_raises(self) -> None:
        intent = ActionIntent(
            hash="xyz",
            action_type=ActionType.no_op,
            target="k8s:demo/svc",
            expected_effect="noop",
            rollback_hint="noop",
            signer="planner",
            signature="",
            expires_at=_EXPIRES,
        )
        with pytest.raises(IntentVerificationError):
            _make_verifier().verify(intent)

    def test_wrong_key_raises(self) -> None:
        # Sign with the test key, then verify with a different public key.
        intent = _make_intent()
        other_priv, other_pub = generate_test_keypair()
        os.environ["PLANNER_VERIFY_KEY"] = other_pub
        wrong_verifier = Verifier()
        with pytest.raises(IntentVerificationError):
            wrong_verifier.verify(intent)
        # Restore the standard test key.
        os.environ["PLANNER_VERIFY_KEY"] = _PUBLIC_PEM


# ---------------------------------------------------------------------------
# Preflight unit tests
# ---------------------------------------------------------------------------


class TestPreflight:
    def test_no_intents_confirmed_hyp_parameter_drift(self) -> None:
        state = _state(hypotheses=[_confirmed_hyp()])
        result = check_preflight(state, [])
        assert result.outcome == PreflightOutcome.parameter_drift

    def test_no_intents_no_hyps_already_resolved(self) -> None:
        state = _state()
        result = check_preflight(state, [])
        assert result.outcome == PreflightOutcome.already_resolved

    def test_no_intents_open_hyp_parameter_drift(self) -> None:
        state = _state(hypotheses=[_open_hyp()])
        result = check_preflight(state, [])
        assert result.outcome == PreflightOutcome.parameter_drift

    def test_all_refuted_diagnosis_invalidated(self) -> None:
        state = _state(hypotheses=[_refuted_hyp()])
        intent = _make_intent()
        result = check_preflight(state, [intent])
        assert result.outcome == PreflightOutcome.diagnosis_invalidated

    def test_open_hypothesis_no_confirmed_parameter_drift(self) -> None:
        state = _state(hypotheses=[_open_hyp()])
        intent = _make_intent()
        result = check_preflight(state, [intent])
        assert result.outcome == PreflightOutcome.parameter_drift

    def test_confirmed_hypothesis_ok(self) -> None:
        state = _state(hypotheses=[_confirmed_hyp()])
        intent = _make_intent()
        result = check_preflight(state, [intent])
        assert result.outcome == PreflightOutcome.ok

    def test_empty_hypotheses_already_resolved(self) -> None:
        state = _state(hypotheses=[])
        intent = _make_intent()
        result = check_preflight(state, [intent])
        assert result.outcome == PreflightOutcome.already_resolved


# ---------------------------------------------------------------------------
# IdempotentExecutor unit tests
# ---------------------------------------------------------------------------


class TestIdempotentExecutor:
    @pytest.mark.asyncio
    async def test_first_execution_succeeds(self) -> None:
        executor = IdempotentExecutor()
        intent = _make_intent()
        record, action = await executor.execute(intent)
        assert record.outcome == "dry_run_success"
        assert action.status == ActionStatus.succeeded
        assert action.intent_hash == intent.hash

    @pytest.mark.asyncio
    async def test_second_call_skips_idempotent(self) -> None:
        executor = IdempotentExecutor()
        intent = _make_intent()
        await executor.execute(intent)
        record2, action2 = await executor.execute(intent)
        assert record2.outcome == "skipped_idempotent"
        assert action2.status == ActionStatus.succeeded

    @pytest.mark.asyncio
    async def test_seeded_hash_skips_immediately(self) -> None:
        intent = _make_intent()
        executor = IdempotentExecutor(already_executed={intent.hash})
        record, _action = await executor.execute(intent)
        assert record.outcome == "skipped_idempotent"

    @pytest.mark.asyncio
    async def test_compensate_records_rollback_type(self) -> None:
        executor = IdempotentExecutor()
        intent = _make_intent()
        record, action = await executor.compensate(intent)
        assert record.is_compensation is True
        assert record.action_type == ActionType.rollback
        assert action.type == ActionType.rollback

    @pytest.mark.asyncio
    async def test_execution_record_is_typed(self) -> None:
        executor = IdempotentExecutor()
        intent = _make_intent()
        record, _ = await executor.execute(intent)
        assert isinstance(record, ExecutionRecord)
        serialised = json.dumps(record.model_dump(mode="json"))
        assert intent.hash in serialised


# ---------------------------------------------------------------------------
# EscalationPackage builder unit tests
# ---------------------------------------------------------------------------


class TestEscalationPackageBuilder:
    def test_builds_with_confirmed_hypothesis(self) -> None:
        state = _state(hypotheses=[_confirmed_hyp()])
        pkg = build_escalation_package(state, failure_reasons=["execution failed"])
        assert pkg.incident_id == "inc_test"
        assert len(pkg.hypotheses_considered) == 1
        assert "confirmed" in pkg.current_working_theory.lower()
        assert pkg.attempt_failure_reasons == ["execution failed"]

    def test_builds_with_open_hypothesis(self) -> None:
        state = _state(hypotheses=[_open_hyp()])
        pkg = build_escalation_package(state)
        assert "unconfirmed" in pkg.current_working_theory.lower()

    def test_builds_with_no_hypotheses(self) -> None:
        state = _state()
        pkg = build_escalation_package(state)
        assert "no hypothesis" in pkg.current_working_theory.lower()

    def test_serialises_to_json_cleanly(self) -> None:
        state = _state(hypotheses=[_confirmed_hyp(), _open_hyp(), _refuted_hyp()])
        pkg = build_escalation_package(
            state,
            failure_reasons=["sig failure"],
            extra_next_steps=["check quota"],
        )
        raw = pkg.model_dump_json()
        parsed = json.loads(raw)
        assert parsed["incident_id"] == "inc_test"
        assert isinstance(parsed["hypotheses_considered"], list)
        assert isinstance(parsed["suggested_next_steps"], list)
        assert isinstance(parsed["attempt_failure_reasons"], list)
        assert isinstance(parsed["current_working_theory"], str)

    def test_rollback_actions_add_next_step(self) -> None:
        rollback_action = Action(
            id="r1",
            type=ActionType.rollback,
            description="undo",
            status=ActionStatus.succeeded,
            executed_at=_NOW,
        )
        state = _state(hypotheses=[_confirmed_hyp()], actions=[rollback_action])
        pkg = build_escalation_package(state, actions_taken=[rollback_action])
        assert any(
            "compensat" in s.lower() or "rollback" in s.lower()
            for s in pkg.suggested_next_steps
        )

    def test_every_field_present(self) -> None:
        state = _state(hypotheses=[_confirmed_hyp()])
        pkg = build_escalation_package(state)
        assert pkg.incident_id
        assert isinstance(pkg.hypotheses_considered, list)
        assert isinstance(pkg.key_findings, list)
        assert isinstance(pkg.attempts, list)
        assert isinstance(pkg.attempt_failure_reasons, list)
        assert pkg.current_working_theory
        assert isinstance(pkg.suggested_next_steps, list)


# ---------------------------------------------------------------------------
# coordinator_node integration-style tests (deterministic, no LLM)
# ---------------------------------------------------------------------------


class TestCoordinatorNode:
    """Tests inject ``_make_verifier()`` so valid intents are accepted and bad-
    signature tests use the same key pair."""

    @pytest.mark.asyncio
    async def test_signature_failure_escalates(self) -> None:
        intent = _make_intent(bad_signature=True)
        state = _state(hypotheses=[_confirmed_hyp()], intents=[intent])
        result = await run_coordinator(state, verifier=_make_verifier())
        assert result["phase"] == IncidentPhase.escalated
        timeline = result["timeline"]
        assert isinstance(timeline, list)
        event_types = [e.event_type for e in timeline]
        assert "coordinator.verification_failed" in event_types

    @pytest.mark.asyncio
    async def test_no_intents_escalates(self) -> None:
        state = _state(hypotheses=[_confirmed_hyp()], intents=[])
        result = await run_coordinator(state, verifier=_make_verifier())
        assert result["phase"] == IncidentPhase.escalated

    @pytest.mark.asyncio
    async def test_no_intents_no_hyps_coordinator_resolved(self) -> None:
        state = _state(hypotheses=[], intents=[])
        result = await run_coordinator(state, verifier=_make_verifier())
        assert result["phase"] == IncidentPhase.resolved

    @pytest.mark.asyncio
    async def test_all_refuted_routes_investigating(self) -> None:
        intent = _make_intent()
        state = _state(hypotheses=[_refuted_hyp()], intents=[intent])
        result = await run_coordinator(state, verifier=_make_verifier())
        assert result["phase"] == IncidentPhase.investigating

    @pytest.mark.asyncio
    async def test_open_hyp_only_routes_planning(self) -> None:
        intent = _make_intent()
        state = _state(hypotheses=[_open_hyp()], intents=[intent])
        result = await run_coordinator(state, verifier=_make_verifier())
        assert result["phase"] == IncidentPhase.planning

    @pytest.mark.asyncio
    async def test_no_hypotheses_already_resolved(self) -> None:
        intent = _make_intent()
        state = _state(hypotheses=[], intents=[intent])
        result = await run_coordinator(state, verifier=_make_verifier())
        assert result["phase"] == IncidentPhase.resolved
        actions = result["actions_taken"]
        assert isinstance(actions, list)
        assert any(a.type == ActionType.no_op for a in actions)

    @pytest.mark.asyncio
    async def test_confirmed_hyp_with_valid_intent_resolves(self) -> None:
        intent = _make_intent()
        state = _state(hypotheses=[_confirmed_hyp()], intents=[intent])
        result = await run_coordinator(state, verifier=_make_verifier())
        assert result["phase"] == IncidentPhase.resolved

    @pytest.mark.asyncio
    async def test_actions_recorded_on_success(self) -> None:
        intent = _make_intent()
        state = _state(hypotheses=[_confirmed_hyp()], intents=[intent])
        result = await run_coordinator(state, verifier=_make_verifier())
        actions = result["actions_taken"]
        assert isinstance(actions, list)
        assert len(actions) >= 1
        assert any(a.intent_hash == intent.hash for a in actions)

    @pytest.mark.asyncio
    async def test_timeline_has_coordinator_events(self) -> None:
        intent = _make_intent()
        state = _state(hypotheses=[_confirmed_hyp()], intents=[intent])
        result = await run_coordinator(state, verifier=_make_verifier())
        timeline = result["timeline"]
        assert isinstance(timeline, list)
        assert any(e.event_type.startswith("coordinator.") for e in timeline)

    @pytest.mark.asyncio
    async def test_state_dict_has_no_unknown_fields(self) -> None:
        intent = _make_intent()
        state = _state(hypotheses=[_confirmed_hyp()], intents=[intent])
        result = await run_coordinator(state, verifier=_make_verifier())
        allowed = set(IncidentState.model_fields)
        for key in result:
            assert key in allowed, f"Unexpected key in coordinator result: {key!r}"

    def test_coordinator_node_sync_wrapper_compiles(self) -> None:
        """The LangGraph-facing sync wrapper exists and is callable."""
        assert callable(coordinator_node)
