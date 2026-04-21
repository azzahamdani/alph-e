"""Unit tests for agent.orchestrator.nodes.planner.

Coverage:
  - confidence gate: confidence < 0.5 forces type=none, no intent.
  - intent presence rules per plan type:
      - rollback, scale, flag_flip → signed ActionIntent in action_intents.
      - pr, runbook, none → no intent.
  - signature validity: signed intent verifies with F-05 test keypair.
  - phase transitions to `planning`.
  - TimelineEvent emitted.

No real LLM calls — ``Client.complete`` is mocked to return a canned
tool-use message that encodes the desired ``_LLMPlannerDecision`` payload.
The payload uses only primitive types so Pydantic strict-mode validation
passes without coercion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.orchestrator.nodes.planner import (
    _CONFIDENCE_THRESHOLD,
    planner_node,
)
from agent.schemas import (
    ActionType,
    Alert,
    IncidentPhase,
    IncidentState,
    RemediationPlan,
    RemediationType,
    Severity,
    TimelineEvent,
)
from agent.security.action_intent import Verifier, generate_test_keypair

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


def _make_state(incident_id: str = "inc_test") -> IncidentState:
    return IncidentState(
        incident_id=incident_id,
        alert=Alert(
            source="alertmanager",
            raw_message="PodOOMKilled on leaky-service",
            service="leaky-service",
            severity=Severity.high,
            fired_at=_NOW,
        ),
        phase=IncidentPhase.investigating,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _llm_payload(
    plan_type: str,
    confidence: float,
    plan_id: str = "plan_test",
    rationale: str = "Test rationale",
    requires_human_approval: bool = True,
    action_target: str = "k8s:demo/leaky-service",
    action_parameters: dict[str, Any] | None = None,
    expected_effect: str = "Restore healthy state.",
    rollback_hint: str = "kubectl rollout undo deployment/leaky-service",
    reasoning: str = "Test reasoning.",
    evidence_ids: list[str] | None = None,
    target_services: list[str] | None = None,
) -> dict[str, Any]:
    """Build the raw primitive payload a mocked LLM would return.

    All fields are primitive Python types so that Pydantic strict-mode
    validation of ``_LLMPlannerDecision`` succeeds without coercion.
    """
    return {
        "plan_id": plan_id,
        "plan_type": plan_type,
        "rationale": rationale,
        "confidence": confidence,
        "requires_human_approval": requires_human_approval,
        "evidence_ids": evidence_ids or [],
        "target_services": target_services or ["leaky-service"],
        "target_repos": [],
        "rollback_plan": "",
        "action_target": action_target,
        "action_parameters": action_parameters or {},
        "expected_effect": expected_effect,
        "rollback_hint": rollback_hint,
        "reasoning": reasoning,
    }


def _make_tool_use_message(tool_name: str, payload: dict[str, Any]) -> MagicMock:
    """Build a fake ``anthropic.types.Message`` with a single ToolUseBlock."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = payload

    msg = MagicMock()
    msg.content = [block]
    msg.usage = MagicMock(
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return msg


def _mock_client(payload: dict[str, Any]) -> MagicMock:
    """Return a mock ``Client`` whose ``complete()`` returns a single canned message."""
    msg = _make_tool_use_message("_llmplannerdecision", payload)
    client = MagicMock()
    client.complete = AsyncMock(return_value=msg)
    return client


@pytest.fixture()
def keypair_env(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Generate a fresh Ed25519 keypair and set env vars for Signer/Verifier."""
    private_pem, public_pem = generate_test_keypair()
    monkeypatch.setenv("PLANNER_SIGNING_KEY", private_pem)
    monkeypatch.setenv("PLANNER_VERIFY_KEY", public_pem)
    return private_pem, public_pem


# ---------------------------------------------------------------------------
# Confidence gate tests
# ---------------------------------------------------------------------------


class TestConfidenceGate:
    @pytest.mark.asyncio
    async def test_low_confidence_forces_none(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """Confidence < 0.5 must produce type=none regardless of LLM choice."""
        payload = _llm_payload("rollback", confidence=0.3)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        assert result["phase"] == IncidentPhase.planning
        final_plan: RemediationPlan = result["remediation_plan"]  # type: ignore[assignment]
        assert final_plan.type == RemediationType.none
        assert "confidence gate" in final_plan.rationale

    @pytest.mark.asyncio
    async def test_low_confidence_no_intent(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """Confidence gate must also suppress the ActionIntent."""
        payload = _llm_payload("scale", confidence=0.49)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        assert result["action_intents"] == []

    @pytest.mark.asyncio
    async def test_threshold_confidence_passes_gate(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """Confidence exactly at threshold must NOT be gated (gate is strict <)."""
        payload = _llm_payload(
            "rollback",
            confidence=_CONFIDENCE_THRESHOLD,
            action_parameters={"replicas": 1},
        )
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        final_plan: RemediationPlan = result["remediation_plan"]  # type: ignore[assignment]
        assert final_plan.type == RemediationType.rollback

    @pytest.mark.asyncio
    async def test_zero_confidence_none_type_unchanged(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """type=none with confidence=0 stays none (gate still applies but is a no-op)."""
        payload = _llm_payload("none", confidence=0.0)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        final_plan: RemediationPlan = result["remediation_plan"]  # type: ignore[assignment]
        assert final_plan.type == RemediationType.none


# ---------------------------------------------------------------------------
# Intent presence rules per plan type
# ---------------------------------------------------------------------------


class TestIntentPresenceRules:
    @pytest.mark.parametrize(
        "plan_type",
        [RemediationType.rollback, RemediationType.scale, RemediationType.flag_flip],
    )
    @pytest.mark.asyncio
    async def test_mutating_type_includes_signed_intent(
        self,
        plan_type: RemediationType,
        keypair_env: tuple[str, str],
    ) -> None:
        """Mutating types must produce a signed ActionIntent in action_intents."""
        payload = _llm_payload(
            plan_type.value,
            confidence=0.9,
            action_parameters={"replicas": 1},
        )
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        intents: list[Any] = result["action_intents"]  # type: ignore[assignment]
        assert len(intents) == 1
        intent = intents[0]
        # Signature must be populated (not the unsigned stub placeholder).
        assert intent.signature != "__unsigned__"
        assert intent.hash != "__unsigned__"

    @pytest.mark.asyncio
    async def test_pr_type_no_intent(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """type=pr must NOT produce an ActionIntent."""
        payload = _llm_payload("pr", confidence=0.8, requires_human_approval=False)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        assert result["action_intents"] == []

    @pytest.mark.asyncio
    async def test_runbook_type_no_intent(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """type=runbook must NOT produce an ActionIntent."""
        payload = _llm_payload("runbook", confidence=0.7)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        assert result["action_intents"] == []

    @pytest.mark.asyncio
    async def test_none_type_no_intent(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """type=none must NOT produce an ActionIntent."""
        payload = _llm_payload("none", confidence=0.0)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        assert result["action_intents"] == []


# ---------------------------------------------------------------------------
# Signature validity
# ---------------------------------------------------------------------------


class TestSignatureValidity:
    @pytest.mark.asyncio
    async def test_rollback_intent_verifies_with_test_keypair(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """The signed ActionIntent for a rollback plan must pass Ed25519 verification."""
        payload = _llm_payload(
            "rollback",
            confidence=0.85,
            action_target="k8s:demo/leaky-service",
            action_parameters={"replicas": 1},
            expected_effect="Restore deployment to previous healthy revision.",
            rollback_hint="kubectl rollout undo deployment/leaky-service -n demo",
        )
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        intents: list[Any] = result["action_intents"]  # type: ignore[assignment]
        assert len(intents) == 1

        verifier = Verifier()
        assert verifier.verify(intents[0]) is True

    @pytest.mark.asyncio
    async def test_scale_intent_verifies(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """The signed ActionIntent for a scale plan must pass verification."""
        payload = _llm_payload(
            "scale",
            confidence=0.75,
            action_target="k8s:demo/leaky-service",
            action_parameters={"replicas": 3},
            expected_effect="Increase replicas to absorb load.",
            rollback_hint="kubectl scale --replicas=1 deployment/leaky-service -n demo",
        )
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        intents: list[Any] = result["action_intents"]  # type: ignore[assignment]
        verifier = Verifier()
        assert verifier.verify(intents[0]) is True

    @pytest.mark.asyncio
    async def test_flag_flip_intent_verifies(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """The signed ActionIntent for a flag_flip plan must pass verification."""
        payload = _llm_payload(
            "flag_flip",
            confidence=0.8,
            action_parameters={"flag": "kill_switch_enabled", "value": True},
            expected_effect="Disable the kill-switch to restore normal flow.",
            rollback_hint="Set kill_switch_enabled=true to re-enable.",
        )
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        intents: list[Any] = result["action_intents"]  # type: ignore[assignment]
        verifier = Verifier()
        assert verifier.verify(intents[0]) is True

    @pytest.mark.asyncio
    async def test_intent_action_type_matches_plan_type(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """``ActionIntent.action_type`` must correspond to the plan type."""
        payload = _llm_payload(
            "flag_flip",
            confidence=0.8,
            action_parameters={"flag": "kill_switch_enabled", "value": True},
        )
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        intents: list[Any] = result["action_intents"]  # type: ignore[assignment]
        assert intents[0].action_type == ActionType.flag_flip

    @pytest.mark.asyncio
    async def test_rollback_intent_action_type(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """``ActionIntent.action_type`` for rollback plan is ``ActionType.rollback``."""
        payload = _llm_payload("rollback", confidence=0.9)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        intents: list[Any] = result["action_intents"]  # type: ignore[assignment]
        assert intents[0].action_type == ActionType.rollback

    @pytest.mark.asyncio
    async def test_plan_requires_human_approval_default_true(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """Mutable plans must have ``requires_human_approval=True``."""
        payload = _llm_payload("rollback", confidence=0.9, requires_human_approval=True)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        final_plan: RemediationPlan = result["remediation_plan"]  # type: ignore[assignment]
        assert final_plan.requires_human_approval is True

    @pytest.mark.asyncio
    async def test_intent_approval_status_pending(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """Freshly created ActionIntents must have ``approval_status='pending'``."""
        payload = _llm_payload("rollback", confidence=0.9)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        intents: list[Any] = result["action_intents"]  # type: ignore[assignment]
        assert intents[0].approval_status == "pending"

    @pytest.mark.asyncio
    async def test_intent_expires_in_future(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """``ActionIntent.expires_at`` must be ~15 minutes in the future."""
        payload = _llm_payload("rollback", confidence=0.9)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        intents: list[Any] = result["action_intents"]  # type: ignore[assignment]
        now = datetime.now(UTC)
        assert intents[0].expires_at > now
        # Allow 1-minute slack for test execution time.
        assert intents[0].expires_at < now + timedelta(minutes=16)


# ---------------------------------------------------------------------------
# State update correctness
# ---------------------------------------------------------------------------


class TestStateUpdate:
    @pytest.mark.asyncio
    async def test_phase_transitions_to_planning(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """Node must advance the phase to ``planning``."""
        payload = _llm_payload("none", confidence=0.0)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        assert result["phase"] == IncidentPhase.planning

    @pytest.mark.asyncio
    async def test_timeline_event_emitted(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """Node must append a ``TimelineEvent`` with the correct actor and type."""
        payload = _llm_payload("none", confidence=0.0)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        timeline: list[TimelineEvent] = result["timeline"]  # type: ignore[assignment]
        assert any(e.actor == "orchestrator:planner" for e in timeline)
        assert any(e.event_type == "planner.decided" for e in timeline)

    @pytest.mark.asyncio
    async def test_timeline_ref_id_is_plan_id(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """The TimelineEvent ref_id must equal the plan_id from the LLM output."""
        payload = _llm_payload("none", confidence=0.0, plan_id="plan_custom_id")
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        timeline: list[TimelineEvent] = result["timeline"]  # type: ignore[assignment]
        planner_events = [e for e in timeline if e.actor == "orchestrator:planner"]
        assert len(planner_events) == 1
        assert planner_events[0].ref_id == "plan_custom_id"

    @pytest.mark.asyncio
    async def test_existing_intents_preserved(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """Pre-existing action_intents must be preserved alongside the new one."""
        from agent.schemas import ActionIntent, ActionType
        from agent.security.action_intent import Signer

        signer = Signer()
        existing_intent = signer.sign(
            ActionIntent(
                hash="__unsigned__",
                action_type=ActionType.no_op,
                target="k8s:demo/other-svc",
                parameters={},
                expected_effect="noop",
                rollback_hint="none",
                signer="orchestrator:coordinator",
                signature="__unsigned__",
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
        )

        state = _make_state()
        state = state.model_copy(update={"action_intents": [existing_intent]})

        payload = _llm_payload("rollback", confidence=0.9, action_parameters={"replicas": 1})
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(state)

        intents: list[Any] = result["action_intents"]  # type: ignore[assignment]
        # Original + new rollback intent.
        assert len(intents) == 2
        hashes = {i.hash for i in intents}
        assert existing_intent.hash in hashes

    @pytest.mark.asyncio
    async def test_updated_at_is_recent(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """``updated_at`` must be set to a recent UTC timestamp."""
        payload = _llm_payload("none", confidence=0.0)
        client = _mock_client(payload)

        before = datetime.now(UTC)
        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())
        after = datetime.now(UTC)

        updated_at: datetime = result["updated_at"]  # type: ignore[assignment]
        assert before <= updated_at <= after

    @pytest.mark.asyncio
    async def test_remediation_plan_in_result(
        self, keypair_env: tuple[str, str]
    ) -> None:
        """``remediation_plan`` key must carry a ``RemediationPlan`` instance."""
        payload = _llm_payload("pr", confidence=0.8, requires_human_approval=False)
        client = _mock_client(payload)

        with patch("agent.orchestrator.nodes.planner.Client", return_value=client):
            result = await planner_node(_make_state())

        assert "remediation_plan" in result
        plan = result["remediation_plan"]
        assert isinstance(plan, RemediationPlan)
        assert plan.type == RemediationType.pr
