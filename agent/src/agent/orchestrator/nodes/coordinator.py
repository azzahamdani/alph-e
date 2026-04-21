"""Coordinator node — lifecycle owner for incident remediation.

Responsibilities (B-07):
1. Verify every ActionIntent via ``Verifier`` BEFORE any side effect (fail CLOSED).
2. Re-check preconditions and route accordingly.
3. Execute intents idempotently (dry-run in MVP1) using the evidence store as
   the idempotency log.
4. On ``type=none`` plan or exhausted-attempts: produce an ``EscalationPackage``
   and move the incident to ``IncidentPhase.escalated``.
5. On partial failure: derive inverse from ``rollback_hint``, execute compensation,
   record both forward and compensating actions, then escalate.

Mutations run under ``KUBECONFIG_AGENT`` env var — never the developer's ambient
kubectl context.

The coordinator is a DETERMINISTIC STATE MACHINE — no LLM calls in MVP1 scope.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Final

from agent.orchestrator.coordinator.escalation import build_escalation_package
from agent.orchestrator.coordinator.exec import IdempotentExecutor
from agent.orchestrator.coordinator.preflight import PreflightOutcome, check_preflight
from agent.schemas import (
    Action,
    ActionStatus,
    ActionType,
    IncidentPhase,
    IncidentState,
    TimelineEvent,
)
from agent.security.action_intent import IntentVerificationError, Verifier

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

_ACTOR: Final[str] = "orchestrator:coordinator"


def _now() -> datetime:
    return datetime.now(UTC)


def _make_event(event_type: str, *, ref_id: str | None = None) -> TimelineEvent:
    return TimelineEvent(ts=_now(), actor=_ACTOR, event_type=event_type, ref_id=ref_id)


def coordinator_node(state: IncidentState) -> dict[str, object]:
    """LangGraph node entry point — synchronous wrapper around the async impl."""
    return asyncio.run(_coordinator_async(state, verifier=None))


async def run_coordinator(
    state: IncidentState,
    *,
    verifier: Verifier | None = None,
) -> dict[str, object]:
    """Async entry for direct invocation and testing.

    Accepts an optional ``Verifier`` instance so tests can inject a known-secret
    verifier without setting environment variables.
    """
    return await _coordinator_async(state, verifier=verifier)


async def _coordinator_async(
    state: IncidentState,
    *,
    verifier: Verifier | None = None,
) -> dict[str, object]:
    timeline = list(state.timeline)
    actions_taken = list(state.actions_taken)
    failure_reasons: list[str] = []

    timeline.append(_make_event("coordinator.started"))

    intents = list(state.action_intents)

    # ------------------------------------------------------------------
    # Step 1 — Signature verification (FAIL CLOSED)
    # ------------------------------------------------------------------
    _verifier = verifier if verifier is not None else Verifier()
    verified_intents = []
    for intent in intents:
        try:
            _verifier.verify(intent)
            verified_intents.append(intent)
        except IntentVerificationError as exc:
            _LOGGER.error(
                "Signature verification failed for intent %r: %s — escalating",
                intent.hash,
                str(exc),
            )
            failure_reasons.append(
                f"Intent {intent.hash!r} failed verification: {str(exc)}"
            )
            # Record a failed action and do NOT retry or re-sign.
            actions_taken.append(
                Action(
                    id=f"verify-fail-{intent.hash[:8]}",
                    type=intent.action_type,
                    description=f"Verification failed: {str(exc)}",
                    status=ActionStatus.failed,
                    intent_hash=intent.hash,
                    executed_at=_now(),
                )
            )
            timeline.append(
                _make_event("coordinator.verification_failed", ref_id=intent.hash)
            )
            # Fail CLOSED: escalate immediately.
            return _escalate(
                state,
                timeline=timeline,
                actions_taken=actions_taken,
                failure_reasons=failure_reasons,
                extra_next_steps=["Review ActionIntent signing pipeline (F-05)."],
            )

    # ------------------------------------------------------------------
    # Step 2 — Precondition re-check
    # ------------------------------------------------------------------
    preflight = check_preflight(state, verified_intents)
    timeline.append(
        _make_event(
            f"coordinator.preflight.{preflight.outcome}",
        )
    )

    match preflight.outcome:
        case PreflightOutcome.diagnosis_invalidated:
            _LOGGER.info("Preflight: diagnosis invalidated — routing to Investigator")
            # The graph routes coordinator → END; signal via phase so upstream
            # routing picks it up on the next iteration.  In MVP1 graph wiring
            # the coordinator is always terminal; callers that need re-routing
            # should inspect the timeline event.
            return {
                "phase": IncidentPhase.investigating,
                "timeline": timeline,
                "updated_at": _now(),
                "actions_taken": actions_taken,
            }

        case PreflightOutcome.parameter_drift:
            if not intents:
                # No intents were produced — the Planner chose type=none.
                # Escalate rather than loop back to planning with no change.
                _LOGGER.info(
                    "Preflight: parameter drift with no intents — type=none plan; escalating"
                )
                failure_reasons.append(
                    "No ActionIntents available (Planner produced type=none or no intents)."
                )
                return _escalate(
                    state,
                    timeline=timeline,
                    actions_taken=actions_taken,
                    failure_reasons=failure_reasons,
                )
            _LOGGER.info("Preflight: parameter drift — routing to Planner")
            return {
                "phase": IncidentPhase.planning,
                "timeline": timeline,
                "updated_at": _now(),
                "actions_taken": actions_taken,
            }

        case PreflightOutcome.already_resolved:
            _LOGGER.info("Preflight: incident already resolved — short-circuiting")
            actions_taken.append(
                Action(
                    id="coordinator-no-op",
                    type=ActionType.no_op,
                    description="Incident self-resolved before coordinator could act.",
                    status=ActionStatus.succeeded,
                    executed_at=_now(),
                )
            )
            timeline.append(_make_event("coordinator.resolved.no_op"))
            return {
                "phase": IncidentPhase.resolved,
                "timeline": timeline,
                "updated_at": _now(),
                "actions_taken": actions_taken,
            }

        case PreflightOutcome.ok:
            pass  # Proceed to execution.

    # ------------------------------------------------------------------
    # Step 3 — Handle type=none or no verified intents (escalate)
    # ------------------------------------------------------------------
    if not verified_intents:
        _LOGGER.info("No verified intents — escalating")
        failure_reasons.append("No executable ActionIntents available after verification.")
        return _escalate(
            state,
            timeline=timeline,
            actions_taken=actions_taken,
            failure_reasons=failure_reasons,
        )

    # ------------------------------------------------------------------
    # Step 4 — Idempotent execution
    # ------------------------------------------------------------------
    executor = IdempotentExecutor()
    execution_failed = False
    failed_intent = None

    for intent in verified_intents:
        try:
            _record, action = await executor.execute(intent)
            actions_taken.append(action)
            timeline.append(
                _make_event("coordinator.executed", ref_id=intent.hash)
            )
        except Exception as exc:
            _LOGGER.exception(
                "Execution failed for intent %r: %s", intent.hash, exc
            )
            failure_reasons.append(
                f"Execution of intent {intent.hash!r} raised: {exc}"
            )
            actions_taken.append(
                Action(
                    id=f"exec-fail-{intent.hash[:8]}",
                    type=intent.action_type,
                    description=f"Execution failed: {exc}",
                    status=ActionStatus.failed,
                    intent_hash=intent.hash,
                    executed_at=_now(),
                )
            )
            timeline.append(
                _make_event("coordinator.execution_failed", ref_id=intent.hash)
            )
            execution_failed = True
            failed_intent = intent
            break

    # ------------------------------------------------------------------
    # Step 5 — Partial-failure compensation
    # ------------------------------------------------------------------
    if execution_failed and failed_intent is not None:
        _LOGGER.info(
            "Partial failure detected for intent %r — executing compensation",
            failed_intent.hash,
        )
        try:
            _comp_record, comp_action = await executor.compensate(failed_intent)
            actions_taken.append(comp_action)
            timeline.append(
                _make_event("coordinator.compensated", ref_id=failed_intent.hash)
            )
        except Exception as comp_exc:
            _LOGGER.exception(
                "Compensation also failed for intent %r: %s",
                failed_intent.hash,
                comp_exc,
            )
            failure_reasons.append(
                f"Compensation for intent {failed_intent.hash!r} also failed: {comp_exc}"
            )
            timeline.append(
                _make_event(
                    "coordinator.compensation_failed", ref_id=failed_intent.hash
                )
            )

        return _escalate(
            state,
            timeline=timeline,
            actions_taken=actions_taken,
            failure_reasons=failure_reasons,
            extra_next_steps=[
                "Verify compensation was effective before retrying.",
            ],
        )

    # ------------------------------------------------------------------
    # All intents executed — move to resolved if no failures, else escalate
    # ------------------------------------------------------------------
    if failure_reasons:
        return _escalate(
            state,
            timeline=timeline,
            actions_taken=actions_taken,
            failure_reasons=failure_reasons,
        )

    timeline.append(_make_event("coordinator.resolved"))
    return {
        "phase": IncidentPhase.resolved,
        "timeline": timeline,
        "updated_at": _now(),
        "actions_taken": actions_taken,
    }


def _escalate(
    state: IncidentState,
    *,
    timeline: list[TimelineEvent],
    actions_taken: list[Action],
    failure_reasons: list[str],
    extra_next_steps: list[str] | None = None,
) -> dict[str, object]:
    """Build an EscalationPackage, record the event, and return the state update.

    The ``EscalationPackage`` is serialised and emitted to the structured log so
    that on-call engineers can retrieve it via log aggregation.  It is NOT stored
    in ``IncidentState`` (schema is read-only per build-fleet rules).
    """
    package = build_escalation_package(
        state,
        failure_reasons=failure_reasons,
        extra_next_steps=extra_next_steps,
        actions_taken=actions_taken,
    )
    _LOGGER.info(
        "Escalating incident %s: %d failure reason(s). EscalationPackage=%s",
        state.incident_id,
        len(failure_reasons),
        package.model_dump_json(),
    )
    timeline.append(
        TimelineEvent(
            ts=_now(),
            actor=_ACTOR,
            event_type="coordinator.escalated",
            ref_id=package.incident_id,
        )
    )
    return {
        "phase": IncidentPhase.escalated,
        "timeline": timeline,
        "updated_at": _now(),
        "actions_taken": actions_taken,
    }
