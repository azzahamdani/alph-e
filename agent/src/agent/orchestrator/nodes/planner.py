"""Planner node — produces a ``RemediationPlan`` and a signed ``ActionIntent``.

Decision discipline (per ``prompts/planner.md`` and B-03 spec):
  - Confidence < 0.5 => ``type=none``.  No PR, no intent.
  - Mutating types (``rollback``, ``scale``, ``flag_flip``) => signed
    ``ActionIntent`` via ``agent.security.action_intent.Signer``.
  - ``type=pr`` => no intent (PR proposes; merge is the human gate).
  - ``type=none`` / ``type=runbook`` => no intent.
  - ``requires_human_approval=True`` is the default for every mutable action.

The LLM is asked to fill a flat primitive model (no nested Pydantic with enums)
so that ``complete_typed``'s strict validation passes cleanly.  The node code
constructs the typed ``RemediationPlan`` and ``ActionIntent`` from those
primitives, then calls ``Signer.sign()``.  The LLM never touches cryptographic
fields.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from pydantic import BaseModel, Field

from agent.llm.client import Client
from agent.llm.settings import LLMSettings
from agent.llm.structured import complete_typed
from agent.orchestrator.nodes._models import PlannerOutput
from agent.prompts.loader import load
from agent.schemas import (
    ActionIntent,
    ActionType,
    IncidentPhase,
    IncidentState,
    RemediationPlan,
    RemediationType,
    TimelineEvent,
)
from agent.security.action_intent import Signer

log = structlog.get_logger(__name__)

# Plan types that require a signed ActionIntent before execution.
_MUTATING_TYPES: frozenset[RemediationType] = frozenset(
    {
        RemediationType.rollback,
        RemediationType.scale,
        RemediationType.flag_flip,
    }
)

# Map RemediationType => ActionType for ActionIntent construction.
_PLAN_TO_ACTION_TYPE: dict[RemediationType, ActionType] = {
    RemediationType.rollback: ActionType.rollback,
    RemediationType.scale: ActionType.scale,
    RemediationType.flag_flip: ActionType.flag_flip,
}

# Default approval window (15 minutes per arch doc safety contract).
_APPROVAL_WINDOW_MINUTES: int = 15

# Confidence threshold below which the node forces type=none.
_CONFIDENCE_THRESHOLD: float = 0.5

# Allowed plan type strings (must match RemediationType values).
_PlanTypeStr = Literal["pr", "rollback", "scale", "flag_flip", "runbook", "none"]


class _LLMPlannerDecision(BaseModel):
    """Flat LLM output: no nested Pydantic models with StrEnum fields.

    ``complete_typed`` uses Pydantic strict-mode validation on the parsed
    JSON; nesting a ``RemediationPlan`` would fail because strict mode rejects
    plain strings for ``StrEnum`` fields.  We use primitives here and
    construct the typed objects in the node code.

    The LLM never touches hash, signature, or any cryptographic field.
    """

    plan_id: str = Field(
        ...,
        description="Stable identifier for this plan, e.g. 'plan_inc_2a91'.",
    )
    plan_type: _PlanTypeStr = Field(
        ...,
        description=(
            "One of: 'pr', 'rollback', 'scale', 'flag_flip', 'runbook', 'none'. "
            "Confidence < 0.5 must produce 'none'."
        ),
    )
    rationale: str = Field(
        ...,
        description="Why this plan type was chosen, citing hypothesis and evidence IDs.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence in the diagnosis and plan (0.0-1.0). < 0.5 forces type=none.",
    )
    requires_human_approval: bool = Field(
        default=True,
        description=(
            "True for all mutable actions.  May be False only for plan_type='pr' "
            "(proposes a PR, does not merge) or plan_type='none'."
        ),
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="Evidence IDs from findings that support this plan.",
    )
    target_services: list[str] = Field(
        default_factory=list,
        description="Services affected by or targeted by this plan.",
    )
    target_repos: list[str] = Field(
        default_factory=list,
        description="Repositories that would be changed by a 'pr' plan.",
    )
    rollback_plan: str = Field(
        default="",
        description="How to undo the plan if it makes things worse.",
    )
    action_target: str = Field(
        default="",
        description=(
            "Fully-qualified target for mutable actions, e.g. "
            "'k8s:demo/leaky-service'.  Required for rollback/scale/flag_flip."
        ),
    )
    action_parameters: dict[str, str | int | bool] = Field(
        default_factory=dict,
        description=(
            "Key-value parameters for the action, e.g. {'replicas': 1}. "
            "Required for rollback/scale/flag_flip."
        ),
    )
    expected_effect: str = Field(
        default="",
        description=(
            "Human-readable expected outcome of the action. "
            "Required for rollback/scale/flag_flip."
        ),
    )
    rollback_hint: str = Field(
        default="",
        description=(
            "How to undo this action if it makes things worse. "
            "Required for rollback/scale/flag_flip."
        ),
    )
    reasoning: str = Field(
        ...,
        description=(
            "One-paragraph explanation of the decision: which hypothesis drove "
            "the plan, why this type was chosen, and what evidence was used."
        ),
    )


def _decision_to_plan(decision: _LLMPlannerDecision) -> RemediationPlan:
    """Construct a ``RemediationPlan`` from the flat LLM decision."""
    return RemediationPlan(
        id=decision.plan_id,
        type=RemediationType(decision.plan_type),
        rationale=decision.rationale,
        confidence=decision.confidence,
        requires_human_approval=decision.requires_human_approval,
        evidence_ids=list(decision.evidence_ids),
        target_services=list(decision.target_services),
        target_repos=list(decision.target_repos),
        rollback_plan=decision.rollback_plan,
    )


def _force_none_plan(plan: RemediationPlan, reason: str) -> RemediationPlan:
    """Return a copy of *plan* coerced to ``type=none`` with *reason* prepended."""
    return plan.model_copy(
        update={
            "type": RemediationType.none,
            "rationale": f"[confidence gate] {reason}  Original rationale: {plan.rationale}",
        }
    )


def _build_intent(
    decision: _LLMPlannerDecision,
    plan: RemediationPlan,
    signer: Signer,
    now: datetime,
) -> ActionIntent:
    """Construct and sign an ``ActionIntent`` for a mutable plan.

    Parameters are taken from the LLM decision; ``Signer.sign()`` populates
    ``hash`` and ``signature`` atomically -- they are never set by hand.
    """
    action_type = _PLAN_TO_ACTION_TYPE[plan.type]
    expires_at = now + timedelta(minutes=_APPROVAL_WINDOW_MINUTES)

    stub = ActionIntent(
        hash="__unsigned__",
        action_type=action_type,
        target=decision.action_target,
        parameters=dict(decision.action_parameters),
        expected_effect=decision.expected_effect,
        rollback_hint=decision.rollback_hint,
        signer="orchestrator:planner",
        signature="__unsigned__",
        expires_at=expires_at,
    )
    return signer.sign(stub)


def _build_user_message(state: IncidentState) -> str:
    """Serialise the relevant ``IncidentState`` slice into a user turn."""
    payload: dict[str, object] = {
        "incident_id": state.incident_id,
        "alert": {
            "service": state.alert.service,
            "severity": state.alert.severity,
            "raw_message": state.alert.raw_message,
        },
        "hypotheses": [
            {
                "id": h.id,
                "text": h.text,
                "score": h.score,
                "status": h.status,
            }
            for h in state.hypotheses
        ],
        "findings": [
            {
                "collector_name": f.collector_name,
                "summary": f.summary,
                "evidence_id": f.evidence_id,
                "confidence": f.confidence,
            }
            for f in state.findings
        ],
        "services_touched": list(state.services_touched),
        "actions_taken": [
            {
                "type": a.type,
                "description": a.description,
                "status": a.status,
            }
            for a in state.actions_taken
        ],
    }
    return (
        "Here is the current incident state. "
        "Produce a RemediationPlan and, for mutable actions, "
        "the required ActionIntent fields.\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    )


async def planner_node(state: IncidentState) -> dict[str, object]:
    """LLM-driven planner: produces a ``RemediationPlan`` + optional signed intent.

    Steps:
    1. Call the LLM (via ``complete_typed``) to get a ``_LLMPlannerDecision``.
    2. Construct a typed ``RemediationPlan`` from primitive LLM output.
    3. Apply confidence gate: confidence < 0.5 forces ``type=none``.
    4. For mutating types, construct and sign an ``ActionIntent`` via
       ``Signer.sign()`` -- the LLM never touches cryptographic fields.
    5. Return the state update dict.
    """
    now = datetime.now(UTC)

    bundle = load("planner")
    system_prompt = f"{bundle.system_prefix}\n\n{bundle.role_prompt}"
    messages = [{"role": "user", "content": _build_user_message(state)}]

    client = Client(LLMSettings())
    decision: _LLMPlannerDecision = await complete_typed(
        client,
        system=system_prompt,
        messages=messages,
        output_model=_LLMPlannerDecision,
        max_retries=1,
    )

    plan = _decision_to_plan(decision)

    if plan.confidence < _CONFIDENCE_THRESHOLD:
        plan = _force_none_plan(
            plan,
            f"Confidence {plan.confidence:.2f} is below threshold {_CONFIDENCE_THRESHOLD}.",
        )

    intent: ActionIntent | None = None
    if plan.type in _MUTATING_TYPES:
        signer = Signer()
        intent = _build_intent(decision, plan, signer, now)

    output = PlannerOutput(plan=plan, intent=intent, reasoning=decision.reasoning)

    new_action_intents = list(state.action_intents)
    if output.intent is not None:
        new_action_intents.append(output.intent)

    timeline = [
        *state.timeline,
        TimelineEvent(
            ts=now,
            actor="orchestrator:planner",
            event_type="planner.decided",
            ref_id=plan.id,
        ),
    ]

    log.info(
        "planner.decided",
        incident_id=state.incident_id,
        plan_type=plan.type,
        confidence=plan.confidence,
        has_intent=intent is not None,
    )

    return {
        "phase": IncidentPhase.planning,
        "timeline": timeline,
        "updated_at": now,
        "action_intents": new_action_intents,
        "remediation_plan": plan,
    }
