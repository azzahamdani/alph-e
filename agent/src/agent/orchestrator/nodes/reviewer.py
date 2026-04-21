"""Reviewer node — PR policy gate.

Gates ``FixProposal`` outputs against hard policy rules (Python, no LLM) and
a soft "challenge or accept" LLM judgement.

Hard rules (checked first — a single failure auto-emits request_changes_on_fix):
1. PR diff must touch only files within ``plan.target_repos``.
2. Commit message must contain the confirmed ``Hypothesis.id``.
3. PR body must reference at least one ``Finding.evidence_id``.

Soft judgement (via Anthropic):
- ``approve`` — PR is correct and safe to merge.
- ``request_changes_on_fix`` — implementation flaw; route back to Dev.
- ``challenge_root_cause`` — root cause is wrong; route back to Investigator.
  Must cite evidence contradicting the hypothesis; empty citations fall back to
  ``request_changes_on_fix``.

Implements WI-011.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import structlog

from agent.llm.client import Client
from agent.llm.structured import complete_typed
from agent.orchestrator.nodes._models import ReviewerOutput
from agent.orchestrator.reviewer.policy import run_all_checks
from agent.prompts import load as load_prompt
from agent.schemas import (
    FixProposal,
    HypothesisStatus,
    IncidentPhase,
    IncidentState,
    RemediationPlan,
    TimelineEvent,
)

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = load_prompt("reviewer")


def _build_review_prompt(
    state: IncidentState,
    proposal: FixProposal,
    plan: RemediationPlan,
) -> str:
    """Render the user-turn context for the soft LLM judgement."""
    confirmed = [h for h in state.hypotheses if h.status == HypothesisStatus.confirmed]
    confirmed_summary = (
        json.dumps([h.model_dump(mode="json") for h in confirmed], indent=2)
        if confirmed
        else "No confirmed hypothesis."
    )

    findings_summary = (
        json.dumps(
            [
                {
                    "id": f.id,
                    "evidence_id": f.evidence_id,
                    "summary": f.summary,
                    "confidence": f.confidence,
                }
                for f in state.findings
            ],
            indent=2,
        )
        if state.findings
        else "No findings."
    )

    changes_summary = json.dumps(
        [
            {
                "repo": c.repo,
                "path": c.path,
                "diff_lines": len(c.diff.splitlines()),
            }
            for c in proposal.changes
        ],
        indent=2,
    )

    return (
        f"## Incident\n"
        f"incident_id: {state.incident_id}\n"
        f"service: {state.alert.service}\n\n"
        f"## Confirmed hypothesis\n{confirmed_summary}\n\n"
        f"## Findings\n{findings_summary}\n\n"
        f"## Remediation plan (target_repos: {plan.target_repos})\n"
        f"plan_id: {plan.id}, rationale: {plan.rationale}\n\n"
        f"## Fix proposal\n"
        f"proposal_id: {proposal.id}\n"
        f"branch: {proposal.branch_name}\n"
        f"commit_message: {proposal.commit_message!r}\n"
        f"pr_body (first 500 chars): {proposal.pr_body[:500]!r}\n"
        f"changes: {changes_summary}\n\n"
        "Review the fix proposal against the confirmed hypothesis and findings. "
        "Return your decision, reasoning, and cited_evidence_ids."
    )


async def reviewer_node(state: IncidentState) -> dict[str, Any]:
    """Gate the FixProposal against policy rules and LLM soft judgement."""
    now = datetime.now(UTC)

    # ------------------------------------------------------------------
    # Retrieve optional fields written by Dev / Planner nodes at runtime.
    # These are not in the typed IncidentState schema because LangGraph
    # passes them via the state dict merger; use getattr with None fallback.
    # ------------------------------------------------------------------
    proposal: FixProposal | None = getattr(state, "fix_proposal", None)
    plan: RemediationPlan | None = getattr(state, "remediation_plan", None)

    if proposal is None or plan is None:
        # Nothing to review — passthrough with a warning timeline entry.
        log.warning(
            "reviewer.no_proposal_or_plan",
            incident_id=state.incident_id,
            has_proposal=proposal is not None,
            has_plan=plan is not None,
        )
        return {
            "phase": IncidentPhase.reviewing,
            "timeline": [
                *state.timeline,
                TimelineEvent(
                    ts=now,
                    actor="orchestrator:reviewer",
                    event_type="reviewer.skipped.no_proposal",
                ),
            ],
            "updated_at": now,
        }

    confirmed = [h for h in state.hypotheses if h.status == HypothesisStatus.confirmed]
    if not confirmed:
        log.warning("reviewer.no_confirmed_hypothesis", incident_id=state.incident_id)
        # Cannot apply commit-message rule — treat as policy violation.
        output = ReviewerOutput(
            decision="request_changes_on_fix",
            reasoning=(
                "Hard policy check failed: no confirmed hypothesis found in state. "
                "Cannot validate commit message references Hypothesis.id."
            ),
            cited_evidence_ids=[],
        )
        return _build_return(state, output, now)

    confirmed_hypothesis = confirmed[0]

    # ------------------------------------------------------------------
    # Hard policy checks — any violation short-circuits the LLM call.
    # ------------------------------------------------------------------
    violations = run_all_checks(proposal, plan, confirmed_hypothesis, state.findings)
    if violations:
        violation_text = "; ".join(f"[{v.rule}] {v.detail}" for v in violations)
        log.info(
            "reviewer.hard_policy_failed",
            incident_id=state.incident_id,
            violations=[v.rule for v in violations],
        )
        output = ReviewerOutput(
            decision="request_changes_on_fix",
            reasoning=f"Hard policy check(s) failed: {violation_text}",
            cited_evidence_ids=[],
        )
        return _build_return(state, output, now)

    # ------------------------------------------------------------------
    # Soft LLM judgement.
    # ------------------------------------------------------------------
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client = Client(api_key=api_key)
    system = f"{_SYSTEM_PROMPT.system_prefix}\n\n{_SYSTEM_PROMPT.role_prompt}"
    user_content = _build_review_prompt(state, proposal, plan)

    raw_output: ReviewerOutput = await complete_typed(
        client,
        system=system,
        messages=[{"role": "user", "content": user_content}],
        output_model=ReviewerOutput,
    )

    # ------------------------------------------------------------------
    # Guardrail: challenge_root_cause requires non-empty citations.
    # ------------------------------------------------------------------
    output = _apply_guardrails(raw_output)

    log.info(
        "reviewer.decision",
        incident_id=state.incident_id,
        decision=output.decision,
        cited_count=len(output.cited_evidence_ids),
    )
    return _build_return(state, output, now)


def _apply_guardrails(output: ReviewerOutput) -> ReviewerOutput:
    """Enforce post-LLM invariants without mutating the frozen model."""
    if output.decision == "challenge_root_cause" and not output.cited_evidence_ids:
        return ReviewerOutput(
            decision="request_changes_on_fix",
            reasoning=(
                f"challenge_root_cause requested but no contradicting evidence cited. "
                f"Falling back to request_changes_on_fix. "
                f"Original reasoning: {output.reasoning}"
            ),
            cited_evidence_ids=[],
        )
    return output


def _build_return(
    state: IncidentState,
    output: ReviewerOutput,
    now: datetime,
) -> dict[str, Any]:
    """Assemble the state-slice dict that the graph reducer will merge."""
    return {
        "phase": IncidentPhase.reviewing,
        "timeline": [
            *state.timeline,
            TimelineEvent(
                ts=now,
                actor="orchestrator:reviewer",
                event_type=f"reviewer.{output.decision}",
            ),
        ],
        "updated_at": now,
        "reviewer_output": output,
    }
