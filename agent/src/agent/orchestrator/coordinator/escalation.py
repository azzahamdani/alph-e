"""EscalationPackage builder.

Produces a structured handoff to on-call humans when:
  - The plan type is ``none`` (no automated remediation possible).
  - All attempts are exhausted.
  - Signature verification failed.
  - Partial execution succeeded but compensation was needed.

The package is designed to give the on-call engineer everything they need to
continue the investigation without re-reading log noise.
"""

from __future__ import annotations

from agent.schemas.escalation import EscalationPackage
from agent.schemas.incident import Action, ActionType, IncidentState


def build_escalation_package(
    state: IncidentState,
    *,
    failure_reasons: list[str] | None = None,
    extra_next_steps: list[str] | None = None,
    actions_taken: list[Action] | None = None,
) -> EscalationPackage:
    """Build an ``EscalationPackage`` from the current incident state.

    Parameters
    ----------
    state:
        Full ``IncidentState`` snapshot at escalation time.
    failure_reasons:
        Ordered list of reasons why automated remediation could not complete.
        Each entry should be a concise one-liner (feeds the 'attempts' column
        in the on-call runbook).
    extra_next_steps:
        Any additional next-step suggestions beyond what the heuristics derive
        from the state (e.g. "check quota limits" added by the verifier).
    actions_taken:
        Override the actions list.  When ``None``, uses ``state.actions_taken``.
    """
    reasons = failure_reasons or []
    effective_actions = actions_taken if actions_taken is not None else list(state.actions_taken)

    # Derive working theory from the highest-scoring confirmed hypothesis, or
    # fall back to the highest-scoring open hypothesis, or a generic statement.
    confirmed = sorted(
        [h for h in state.hypotheses if h.status == "confirmed"],
        key=lambda h: h.score,
        reverse=True,
    )
    open_hyps = sorted(
        [h for h in state.hypotheses if h.status == "open"],
        key=lambda h: h.score,
        reverse=True,
    )

    if confirmed:
        working_theory = (
            f"Confirmed root cause: {confirmed[0].text} "
            f"(score={confirmed[0].score:.2f}). "
            "Automated remediation was not possible; human action required."
        )
    elif open_hyps:
        working_theory = (
            f"Most likely root cause (unconfirmed): {open_hyps[0].text} "
            f"(score={open_hyps[0].score:.2f}). "
            "Further investigation is needed before remediation."
        )
    else:
        working_theory = (
            "No hypothesis was confirmed during automated investigation. "
            f"Alert: {state.alert.raw_message} (service={state.alert.service})."
        )

    # Derive suggested next steps from hypotheses and failure reasons.
    steps: list[str] = []
    if confirmed:
        steps.append(
            f"Review confirmed hypothesis: {confirmed[0].text}"
        )
    for hyp in open_hyps[:2]:  # Top-2 open hypotheses.
        steps.append(f"Investigate open hypothesis: {hyp.text} (score={hyp.score:.2f})")

    if reasons:
        steps.append("Review automated attempt failure reasons listed above.")

    # Surface any services that were touched.
    if state.services_touched:
        steps.append(
            f"Inspect recently touched services: {', '.join(state.services_touched)}"
        )

    # Add compensation hint if any rollback was performed.
    rollback_actions = [a for a in effective_actions if a.type == ActionType.rollback]
    if rollback_actions:
        steps.append(
            "A compensating rollback was performed — verify the system returned to a known-good state."
        )

    steps.extend(extra_next_steps or [])

    if not steps:
        steps.append("Manually review the incident timeline and alert labels.")

    # MVP1: no separate remediation-plan list on IncidentState (schema read-only).
    # Leave attempts empty; callers inject plans if available.
    from agent.schemas.remediation import RemediationPlan  # noqa: PLC0415

    attempted_plans: list[RemediationPlan] = []

    return EscalationPackage(
        incident_id=state.incident_id,
        hypotheses_considered=list(state.hypotheses),
        key_findings=list(state.findings),
        attempts=attempted_plans,
        attempt_failure_reasons=reasons,
        current_working_theory=working_theory,
        suggested_next_steps=steps,
    )
