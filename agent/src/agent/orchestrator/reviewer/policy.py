"""Hard policy checks for the Reviewer node.

These checks run in Python with no LLM calls.  Each check returns a
``PolicyViolation`` dataclass when the rule is broken, or ``None`` on success.
All violations short-circuit the soft LLM judgement: the reviewer emits
``request_changes_on_fix`` with the violation reason in ``reasoning``.

Rules (per B-06 spec):
1. PR diff must touch only files within ``plan.target_repos``.
2. Commit message must contain the confirmed ``Hypothesis.id``.
3. PR body must reference at least one ``Finding.evidence_id``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agent.schemas.collector import Finding
from agent.schemas.incident import Hypothesis
from agent.schemas.remediation import FixProposal, RemediationPlan


@dataclass(frozen=True)
class PolicyViolation:
    """A hard policy rule that the PR failed."""

    rule: str
    detail: str


def check_diff_repos(
    proposal: FixProposal,
    plan: RemediationPlan,
) -> PolicyViolation | None:
    """Rule 1: every changed file's repo must be in ``plan.target_repos``.

    Returns ``None`` when the rule passes; a ``PolicyViolation`` otherwise.
    """
    if not plan.target_repos:
        # If the plan has no target repos the check cannot meaningfully pass;
        # treat this as a violation so the PR is sent back for clarification.
        return PolicyViolation(
            rule="diff_repos",
            detail=(
                "RemediationPlan.target_repos is empty — cannot validate that "
                "changed files are within approved repositories."
            ),
        )

    allowed = set(plan.target_repos)
    out_of_scope = {change.repo for change in proposal.changes if change.repo not in allowed}

    if out_of_scope:
        return PolicyViolation(
            rule="diff_repos",
            detail=(
                f"PR touches repos outside plan.target_repos. "
                f"Out-of-scope repos: {sorted(out_of_scope)}. "
                f"Allowed: {sorted(allowed)}."
            ),
        )
    return None


def check_commit_message(
    proposal: FixProposal,
    confirmed_hypothesis: Hypothesis,
) -> PolicyViolation | None:
    """Rule 2: commit message must contain the confirmed hypothesis ID.

    Returns ``None`` when the rule passes; a ``PolicyViolation`` otherwise.
    """
    if confirmed_hypothesis.id not in proposal.commit_message:
        return PolicyViolation(
            rule="commit_message",
            detail=(
                f"Commit message does not reference the confirmed hypothesis ID "
                f"'{confirmed_hypothesis.id}'. "
                f"Commit message: {proposal.commit_message!r}."
            ),
        )
    return None


def check_pr_body_evidence(
    proposal: FixProposal,
    findings: Sequence[Finding],
) -> PolicyViolation | None:
    """Rule 3: PR body must reference at least one Finding.evidence_id.

    Returns ``None`` when the rule passes; a ``PolicyViolation`` otherwise.
    """
    evidence_ids = {f.evidence_id for f in findings}

    if not evidence_ids:
        return PolicyViolation(
            rule="pr_body_evidence",
            detail=(
                "No findings are present in IncidentState — cannot verify that "
                "the PR body references a known evidence_id."
            ),
        )

    if not any(eid in proposal.pr_body for eid in evidence_ids):
        return PolicyViolation(
            rule="pr_body_evidence",
            detail=(
                "PR body does not reference any Finding.evidence_id. "
                f"Known evidence IDs: {sorted(evidence_ids)}."
            ),
        )
    return None


def run_all_checks(
    proposal: FixProposal,
    plan: RemediationPlan,
    confirmed_hypothesis: Hypothesis,
    findings: Sequence[Finding],
) -> list[PolicyViolation]:
    """Run all hard policy checks and return the list of violations (may be empty)."""
    violations: list[PolicyViolation] = []

    v1 = check_diff_repos(proposal, plan)
    if v1 is not None:
        violations.append(v1)

    v2 = check_commit_message(proposal, confirmed_hypothesis)
    if v2 is not None:
        violations.append(v2)

    v3 = check_pr_body_evidence(proposal, findings)
    if v3 is not None:
        violations.append(v3)

    return violations
