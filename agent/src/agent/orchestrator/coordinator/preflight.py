"""Pre-execution precondition re-check.

Before the coordinator touches anything, it re-reads the live state and decides
whether the Planner's diagnosis is still valid.

Three outcomes (see B-07 spec):
  - ``diagnosis_invalidated`` — something has changed that invalidates the root
    cause.  Route back to Investigator.
  - ``parameter_drift``      — the target is still broken but parameters have
    shifted.  Route back to Planner for a fresh ActionIntent.
  - ``already_resolved``     — the incident cleared itself.  Short-circuit to
    ``IncidentPhase.resolved`` with a ``no_op`` action record.

MVP1: The precondition check is a deterministic heuristic over the in-memory
``IncidentState``.  In production this would re-query the kube/metrics APIs.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agent.schemas.incident import ActionIntent, IncidentState


class PreflightOutcome(StrEnum):
    ok = "ok"
    diagnosis_invalidated = "diagnosis_invalidated"
    parameter_drift = "parameter_drift"
    already_resolved = "already_resolved"


class PreflightResult(BaseModel):
    """Typed result of the precondition check."""

    model_config = ConfigDict(frozen=True)

    outcome: PreflightOutcome
    reason: str = Field(default="", description="Human-readable explanation for the outcome.")


def check_preflight(state: IncidentState, intents: list[ActionIntent]) -> PreflightResult:
    """Run precondition checks and return a routing decision.

    Rules applied in order (first match wins):

    1. **No intents** — nothing to execute; treat the same as already-resolved
       if any confirmed hypothesis exists, otherwise parameter_drift to let the
       Planner try again.
    2. **All hypotheses refuted or abandoned** — the diagnosis has been
       invalidated since the Planner ran.  Route back to Investigator.
    3. **No confirmed hypothesis** — diagnosis is uncertain; route back to
       Planner for a fresh intent.
    4. **No open hypotheses and no confirmed ones** — state is empty; already
       resolved is the safest assumption.
    5. **Otherwise** — conditions look stable; proceed with execution.
    """
    if not intents:
        # Nothing to execute.
        if not state.hypotheses:
            # No hypotheses and no intents — nothing for the agent to do.
            return PreflightResult(
                outcome=PreflightOutcome.already_resolved,
                reason="No action intents and no hypotheses — incident appears resolved.",
            )
        # Intents absent with hypotheses present means either the Planner chose
        # type=none (no automated remediation available) or intents were never
        # generated.  Either way, route to parameter_drift so the coordinator
        # escalation path is triggered by the caller.
        return PreflightResult(
            outcome=PreflightOutcome.parameter_drift,
            reason="No action intents present; routing to Planner for a fresh intent.",
        )

    # Check if all hypotheses are terminal-negative (refuted/abandoned).
    if state.hypotheses and all(
        h.status in ("refuted", "abandoned") for h in state.hypotheses
    ):
        return PreflightResult(
            outcome=PreflightOutcome.diagnosis_invalidated,
            reason=(
                "All hypotheses are refuted or abandoned; "
                "the Planner's diagnosis no longer holds."
            ),
        )

    # If there are no confirmed hypotheses and we have some open/unresolved ones,
    # the Planner may have acted on stale data.
    confirmed = [h for h in state.hypotheses if h.status == "confirmed"]
    if not confirmed and state.hypotheses:
        return PreflightResult(
            outcome=PreflightOutcome.parameter_drift,
            reason=(
                "No confirmed hypothesis found at execution time; "
                "routing to Planner for fresh intent with current conditions."
            ),
        )

    # No hypotheses at all — assume self-resolved.
    if not state.hypotheses:
        return PreflightResult(
            outcome=PreflightOutcome.already_resolved,
            reason="No hypotheses in state; incident appears to have self-resolved.",
        )

    return PreflightResult(
        outcome=PreflightOutcome.ok,
        reason="Preconditions satisfied; proceeding with execution.",
    )
